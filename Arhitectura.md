```markdown
# Nota de Arhitectura - Proiect C (Aprovizionare si Stocuri)
**Faza 1: Solutia de baza si Cererea de Modificare 1 (CR-1)**

---

## 1. Descrierea Generala si Componentele Sistemului

Solutia automatizeaza fluxul de reaprovizionare a depozitului pentru compania fictiva Industrii Demo BPA SRL. Aceasta este livrata sub forma de container Docker bazat pe `python:3.12-slim` si interactioneaza exclusiv prin API REST cu serverul Majordom Hub.

Arhitectura este organizata pe 3 straturi functionale integrate in fisierul `solutie.py`:

1. **Stratul de Comunicatie si Rezilienta (`HubClient`)**:
   - Gestioneaza sesiunea HTTP persistenta (`requests.Session`).
   - Implementeaza logica de auto-recuperare: tratarea erorilor tranzitorii (500, 502, 503, 504) prin retry cu backoff exponential si respectarea antetului `Retry-After`.
   - Gestioneaza limitarea de rata (HTTP 429) pana la restabilirea disponibilitatii hub-ului.
   - Injecteaza antetul `Idempotency-Key` pentru operatiunile de scriere idempotente.

2. **Motorul de Reguli de Business (`main`)**:
   - Parcurge mesajele din `/inbox` in ordinea canonica stricta (`received_at`, `request_id`).
   - Consulta starea inventarului (`/inventory/{item_code}`), ofertele furnizorilor (`/offers`) si registrul de parteneri (`/suppliers/{supplier_id}`).
   - Efectueaza calculele zecimale exacte (tipul `Decimal`) pentru cantitati (MOQ, dimensiuni pachet) si costuri convertite in RON pe baza cursurilor valutare.
   - Selecteaza oferta optima pe baza algoritmului configurat (`LOWEST_COST` sau `WEIGHTED_SCORE` in CR-1).

3. **Stratul de Persistenta si Raportare**:
   - Emite comenzile de achizitie (`POST /purchase-orders`).
   - Inregistreaza deciziile finale (`POST /decisions`).
   - Compileaza si transmite raportul final (`POST /report`) la terminarea procesarii tuturor elementelor din inbox.

---

## 2. Maparea Regulilor de Business in Cod

Toate calculele folosesc tipul `Decimal` si rotunjiri `ROUND_HALF_UP` (la 2 zecimale pentru sume si 6 zecimale pentru scorurile normalizate din CR-1).

| Regula | Descriere | Implementare in `solutie.py` | Rezultat / Cod Motiv |
| :--- | :--- | :--- | :--- |
| **C-R01** | Articol necunoscut | Apel `GET /inventory/{item_code}`; daca intoarce 404 | `REJECTED` -> `ITEM_UNKNOWN` |
| **C-R02** | Stoc peste prag | `current_stock > reorder_point` | `NO_ORDER_NEEDED` -> `STOCK_ABOVE_REORDER_POINT` |
| **C-R03** | Necesar initial | `needed_qty = target_stock - current_stock` | Daca `needed_qty <= 0` -> `TARGET_ALREADY_MET` |
| **C-R04** | Ajustare cantitate | `qty = max(needed_qty, moq)`, rotunjit in sus la multiplu de `pack_size` | Ajustare cantitativa interna |
| **C-R05** | Filtrare furnizori | `status IN ('BLOCKED', 'INACTIVE')` din `/suppliers/{id}` | Furnizorul este exclus din selectie |
| **C-R06** | Conversie valutara | `total_cost_ron = round2(qty * unit_price * fx_rates[curr])` | Calcul zecimal exact cu rotunjire la 2 zecimale |
| **C-R07** | Lipsa oferte | Daca lista de oferte eligibile este vida dupa C-R05 | `MANUAL_REVIEW` -> `NO_ELIGIBLE_SUPPLIER` |
| **C-R07 (CR-1)**| Selectie ponderata | Normalizare min-max la 6 zecimale (`norm_cost`, `norm_lead`) + scor ponderat | Selecteaza oferta cu `min(score)` (sau criterii secundare) |
| **C-R08** | Plafon aprobare | `total_cost_ron > approval_limit_ron` (din `/config`) | `MANUAL_REVIEW` -> `ABOVE_APPROVAL_LIMIT` |
| **C-R09** | Fiabilitate scazuta | `on_time_rate < min_on_time_rate` (din `/config`) | `MANUAL_REVIEW` -> `LOW_RELIABILITY` |
| **C-R10** | Plasare comanda | Emitere `POST /purchase-orders` doar pentru ofertele aprobate | `ORDER_PLACED` |
| **C-R11** | Inregistrare decizie| `POST /decisions` apelat pentru fiecare document din inbox | Inregistrare decizie cu cheie unica |
| **C-R12** | Esec definitiv | `POST /purchase-orders` returneaza 500 la toate cele 3 incercari | Document marcat ca `FAILED` in `/report` |
| **C-R13** | Raportul final | `POST /report` trimis o singura data la final cu toate `items` | Incheiere executie cu codul 0 |
| **C-R14** | Deja procesat | Decizie existenta in `/history` cu `source: EARLIER_RUN` | Status `ALREADY_PROCESSED` in raport |

---

## 3. Mecanisme de Rezilienta si Repornire

### A. Reincercari si Toleranta la Erori Tranzitorii
Hub-ul poate injecta pana la 2 erori consecutive (500, 503) si 3 raspunsuri consecutive de tip Rate Limit (429):
- Orice raspuns HTTP `500/502/503/504` este reincercat de pana la 3 ori, cu pauza dictata de antetul `Retry-After` (implicit 1 secunda).
- Daca `POST /purchase-orders` esueaza dupa 3 incercari cu codul 500, se activeaza regula **C-R12** (se considera eroare permanenta injectata), comanda este marcata ca `FAILED`, iar scriptul continua cu urmatorul element din inbox fara a se bloca.
- Raspunsurile `429 Too Many Requests` sunt tratate prin asteptare conform valorii din antetul `Retry-After`, fara a consuma din numarul de retry-uri alocate apelului.

### B. Cheile de Idempotenta
Pentru a preveni efectele laterale la reincercare sau reluare, fiecare cerere de scriere utilizeaza o cheie determinista:
- Comenzi de achizitie: `{request_id}:po`
- Decizii: `{request_id}:decision`
- Raport final: `report` (la reluare sau daca hub-ul returneaza `422 IDEMPOTENCY_KEY_REUSED`, se itereaza pe formatul `report-2`, `report-3` pana la acceptare).

### C. Gestionarea Repornirilor (Crash Recovery)
La fiecare pornire, scriptul citeste complet `/history`:
1. **Decizii din rulari anterioare (`source: EARLIER_RUN`)**: Cererea nu este reevaluata, nu se emit apeluri catre furnizori sau comenzi noi; este inclusa direct in raportul final cu statusul `ALREADY_PROCESSED` (**C-R14**).
2. **Decizii din rularea curenta (`source: THIS_RUN`)**: Indica faptul ca solutia a fost oprita fortat anterior dupa ce decizia a fost luata. Elementul este raportat ca `RECORDED`, fara a retrimite comanda sau decizia.
3. **Fara decizie in istoric**: Cererea este procesata in mod normal, respectand ordinea canonica din `/inbox`.