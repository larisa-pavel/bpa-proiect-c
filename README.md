# Proiect C - Aprovizionare si Stocuri

Solutie automata pentru procesarea cererilor de aprovizionare, compararea ofertelor furnizorilor, generarea comenzilor de achizitie (PO) si transmiterea raportului catre Majordom Hub.

---

## Dependente

Solutia este complet containerizata si nu necesita pachete instalate direct pe sistemul de operare gazda.

- **Imagine de baza Docker**: `python:3.12-slim` (Linux/amd64).
- **Librarii Python** (definite in `requirements.txt`):
  - `requests>=2.31.0`: utilizat pentru apelurile HTTP REST catre Majordom Hub.
- **Instrumente necesare pe masina gazda**:
  - Docker Desktop cu suport WSL 2 activ.
  - Python 3.10+ pentru executarea scriptului de testare `bpa_runner.py`.
  - Git pentru gestiunea versiunilor si marcarea tag-urilor de livrare.

---

## Ipoteze

1. **Ordinea canonica de procesare**: Elementele din `/inbox` sunt consumate strict secvential, sortate dupa `received_at` si apoi dupa `request_id`.
2. **Calcul numeric exact**: Cantitatile (MOQ, pack size), costurile si ponderile sunt evaluate exclusiv folosind clasa `Decimal` (modulul `decimal`), eliminand erorile de aproximare tipice virgulei mobile (`float`). Sumele sunt rotunjite la 2 zecimale prin `ROUND_HALF_UP`, iar scorurile normalizate din CR-1 sunt rotunjite la 6 zecimale[cite: 11, 12].
3. **Idempotenta la nivel de apel**: Orice interactiune cu efecte laterale utilizeaza antetul `Idempotency-Key` cu format determinist:
   - Comenzi de achizitie: `{request_id}:po`
   - Decizii finale: `{request_id}:decision`
   - Raport final: `report` (cu fallback `report-2`, `report-3` in caz de `422 IDEMPOTENCY_KEY_REUSED`).
4. **Rezilienta la erori tranzitorii**: Erorile HTTP `500`, `502`, `503`, `504` si `429` sunt considerate temporare si sunt reincercate automat de pana la 3 ori, respectand antetul `Retry-After`. Daca un apel `POST /purchase-orders` esueaza cu 500 la toate cele 3 incercari, se aplica regula C-R12 (esec definitiv), elementul primind status `FAILED` in raport, permitand continuarea fluxului fara blocaj.
5. **Comportament la repornire (crash recovery)**: La fiecare initializare, solutia interogheaza `/history`:
   - Documentele cu `source: EARLIER_RUN` sunt raportate ca `ALREADY_PROCESSED` fara recalculare (C-R14).
   - Documentele deja decise in rularea curenta (`source: THIS_RUN`) sunt raportate ca `RECORDED` fara retransmitere.
6. **Suport pentru Change Request 1 (CR-1)**: Daca `selection_method` este `WEIGHTED_SCORE`, algoritmul normalizeaza valorile de cost si lead-time prin formula `(x - min) / (max - min)` si aplica ponderile definite in `selection_weights` din `/config`[cite: 11, 12].

---

## Cum se ruleaza

Toate comenzile se executa in terminal (PowerShell pe Windows sau Bash pe Linux/macOS) in directorul radacina al proiectului.

### 1. Configurarea variabilelor de mediu (Autentificare Hub)
Inainte de rularea testelor, este obligatorie setarea variabilelor de mediu pentru conectarea la Majordom Hub:

- **PowerShell (Windows):**
  ```powershell
  $env:BPA_HUB = "[https://apb.majordom.tech](https://apb.majordom.tech)"
  $env:BPA_KEY = "<CHEIA_TA_SECRETA>"
  ```

### 2. Construirea imaginii Docker
Construiti imaginea specificand arhitectura tinta `linux/amd64`:
```powershell
docker build --platform linux/amd64 -t bpa-solutie .
```

### 3. Rularea suitei oficiale de teste
Ruleaza testele publice ale Proiectului C:
```powershell
python bpa_runner.py --project C --suite public -- docker run --rm --network=host bpa-solutie "{base_url}"
```

### 4. Rularea suitei de Change Request (CR-1)
Ruleaza scenariile specifice cererii de modificare 1:
```powershell
python bpa_runner.py --project C --suite change-request --change-request CR-1 -- docker run --rm --network=host bpa-solutie "{base_url}"
```