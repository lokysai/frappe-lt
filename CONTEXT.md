# Frappe / ERPNext Lithuanian Translation

This context defines the Lithuanian language used by the `frappe-lt` translation package and the boundaries used to measure translation completeness.

## Language

### Translation Work

**Source Phrase**:
An English phrase marked by Frappe or ERPNext as translatable.

**Translation Key**:
The combination of `frappe.as_unicode(source).strip()` and its optional **Frappe Context** used to look up one translation, without internal newline or Unicode normalization.
_Avoid_: Phrase, row

**Active Translation Key**:
A **Translation Key** present in the canonical inventory extracted from the Frappe and ERPNext version pair currently being evaluated.
_Avoid_: Historical key, removed key

**Release Inventory**:
The fixed set of **Active Translation Keys** recorded for one released `frappe-lt` compatibility manifest.
_Avoid_: Current target inventory, historical report

**Frappe Context**:
An optional runtime qualifier that lets the same **Source Phrase** have different translations in different uses.
_Avoid_: Source location, module

**Source Location**:
A file, document type, report, page, or other origin that helps the translation agent understand where a **Source Phrase** is used.
_Avoid_: Frappe Context

**Translation Catalog**:
The versioned set of Lithuanian translations and their metadata for one supported Frappe and ERPNext version pair.
_Avoid_: Database import, spreadsheet

**Translation Coverage**:
The share of active **Translation Keys** that have a non-empty Lithuanian translation or an approved **Translation Exception**.
_Avoid_: Number of translated source phrases

**Translation Exception**:
An entry in the reviewed exception list for a product name, abbreviation, technical identifier, or code value that must remain unchanged.
_Avoid_: Untranslated phrase, fallback

<a id="catalog-quality-gate"></a>

**Catalog Quality Gate**:
Vienintelis viešas kandidatinio katalogo generavimo kelias, kuris autentifikuoja **Release Inventory** ir susietus manifestus, patikrina visas technines bei žodyno taisykles ir publikuoja kandidatą tik po izoliuoto Frappe gettext kompiliavimo.
_Vengti_: Atskiras PO tikrintuvas, neautentifikuotas generatorius

<a id="catalog-segment"></a>

**Catalog Segment**:
Versijuotame registre įvardytas nesikertantis **Release Inventory** **Translation Key** poaibis, susietas su tiksliu inventoriaus digestu ir vienu registruotu kandidatu.
_Vengti_: Neužregistruotas vertimų paketas, savavališkas raktų poaibis

<a id="html-equivalence"></a>

**HTML Equivalence**:
Griežtas HTML5 fragmentų lygiavertiškumas, leidžiantis perkelti pilnas gretimas aukščiausio lygio žymų šakas, bet išsaugantis kiekvienos šakos vidinę struktūrą, mixed text/tail ryšius, žymų kiekį, neverčiamus atributus ir URL.
_Vengti_: Naršyklės tyliai pataisytas HTML, vien žymų kiekio palyginimas

<a id="significant-whitespace"></a>

**Significant Whitespace**:
Šaltinio kraštiniai ir kartotiniai tarpai, tabuliacija bei tiksli CR, LF ir CRLF seka kiekvienoje plain string arba suporuotoje HTML text/tail reikšmėje.
_Vengti_: Normalizuoti eilučių lūžiai, visų tarpų ignoravimas

<a id="unknown-token-syntax"></a>

**Unknown Token Syntax**:
Į parametrą ar šablono išraišką panaši sintaksė, likusi pašalinus visas žinomas **Preserved Token** gramatikas ir jų escaped formas; ji blokuoja kandidato generavimą.
_Vengti_: Nežinomo tokeno perspėjimas, paprasto panašaus teksto blokavimas

<a id="glossary-selector"></a>

**Glossary Selector**:
Versijuota reviewed machine-readable taisyklė, kuri pagal tikslų **Translation Key**, source digest, **Frappe Context** ir prireikus stabilias **Source Location** parenka literal accepted bei forbidden formas su aiškia case ir Unicode politika.
_Vengti_: Morphology spėjimas, laisva blokuojanti regex

**English Fallback**:
A visible English **Source Phrase** produced when the effective highest-precedence translation lookup has no approved Lithuanian result, including an English database override that hides a valid application translation.
_Avoid_: Translation Exception

**Inherited Translation**:
A Lithuanian translation carried forward from the Frappe or ERPNext v15 catalogs.

**AI Translation**:
A new or corrected Lithuanian translation produced by an OpenCode agent under the project's glossary and quality rules.

**Suspicious Translation**:
An **Inherited Translation** flagged for a foreign-language fragment, broken grammar, literal wording, inconsistent terminology, damaged punctuation, or changed meaning.

**Critical Translation Error**:
A translation error that can cause a wrong financial or administrative action, confuse a document state, amount, debit or credit, hide a security warning, or block a workflow.
_Avoid_: Style issue, awkward wording

### ERP Terms

<a id="glossary-preke"></a>

**Prekė** (`Item`):
ERPNext įrašas, kuris gali žymėti prekę, paslaugą, žaliavą, gaminį arba turtą.
_Vengti_: Elementas, daiktas, gaminys

**Prekės kodas** (`Item Code`):
Unikalus **Prekę** identifikuojantis kodas.
_Vengti_: Elemento kodas, produkto kodas

**Klientas** (`Customer`):
Asmuo arba organizacija, kuriai įmonė parduoda prekes ar paslaugas ir kurios ryšį valdo pardavimo bei CRM procesuose.
_Vengti_: Pirkėjas, užsakovas

**Tiekėjas** (`Supplier`):
Asmuo arba organizacija, iš kurios įmonė perka prekes arba paslaugas.
_Vengti_: Pardavėjas, teikėjas

**Įmonė** (`Company`):
Juridinis vienetas, kurio apskaita ir veikla valdoma ERPNext.
_Vengti_: Kompanija, bendrovė

**Naudotojas** (`User`):
Asmuo arba techninė tapatybė, galinti prisijungti prie Frappe svetainės arba naudoti jos API.
_Vengti_: Vartotojas, paskyra

**Darbuotojas** (`Employee`):
Įmonėje dirbantis asmuo, kurio darbo ryšys valdomas sistemoje.
_Vengti_: Naudotojas, tarnautojas

**Apskaitos sąskaita** (`Account`, accounting context):
Didžiosios knygos klasifikavimo vienetas, įtrauktas į sąskaitų planą.
_Vengti_: Paskyra, kliento sąskaita

**Paskyra** (`Account`, user context):
Naudotojo tapatybė ir jos prieiga prie sistemos.
_Vengti_: Sąskaita

<a id="saskaita-account-contextless-collision"></a>

**Sąskaita** (`Account`, contextless collision):
Patvirtintas bendras vertimas konteksto neturinčiam raktui, kurį v16 naudoja ir apskaitos dokumente, ir el. pašto paskyros lauke.
Release inventory generation applies this decision only when the pinned runtime metadata proves both `Account` and `Email Account.account_section` locations.
_Vengti_: Paskyra

**Banko sąskaita** (`Bank Account`):
Įmonės, kliento arba tiekėjo sąskaita finansų įstaigoje.
_Vengti_: Banko paskyra

**Pardavimo užsakymas** (`Sales Order`):
ERPNext dokumentas, kuriame registruojamas kliento užsakymas įsigyti nurodytas prekes ar paslaugas.
_Vengti_: Pardavimų užsakymas, užsakymas

**Pirkimo užsakymas** (`Purchase Order`):
ERPNext dokumentas, kuriame registruojamas įmonės užsakymas įsigyti iš tiekėjo nurodytas prekes ar paslaugas.
_Vengti_: Pirkimų užsakymas, užsakymas

**Gamybos užsakymas** (`Work Order`):
Nurodymas pagaminti nustatytą prekės kiekį pagal gaminio specifikaciją.
_Vengti_: Darbo užsakymas, darbų užsakymas

**Rikiavimo tvarka** (`Sort Order`):
Taisyklė, pagal kurią sąrašo reikšmės išdėstomos nustatyta seka.
_Vengti_: Užsakymas, rūšiavimo užsakymas

**Vieneto kaina** (`Rate`, item pricing context):
Vienos prekės arba paslaugos vieneto pardavimo ar pirkimo kaina.
_Vengti_: Kursas, norma

**Valiutos kursas** (`Exchange Rate`):
Vienos valiutos vertės santykis su kita valiuta.
_Vengti_: Valiutos įkainis, keitimo norma

**Mokesčio tarifas** (`Tax Rate`):
Mokesčiui apskaičiuoti taikomas dydis.
_Vengti_: Mokesčio kursas, mokesčio norma

**Valandinis įkainis** (`Hourly Rate`):
Už vieną darbo valandą taikoma kaina.
_Vengti_: Valandinis kursas

**Apskaitinė vieneto vertė** (`Valuation Rate`):
Atsargų apskaitoje vienam prekės vienetui priskirta vertė.
_Vengti_: Vertinimo norma, vertinimo kursas

**Pardavimo grąžinimas** (`Sales Return`):
Pardavimo operaciją visiškai arba iš dalies panaikinantis prekių ar sumų grąžinimo dokumentas.
_Vengti_: Pardavimo grįžimas, pardavimo grąža

**Pirkimo grąžinimas** (`Purchase Return`):
Pirkimo operaciją visiškai arba iš dalies panaikinantis prekių ar sumų grąžinimo dokumentas.
_Vengti_: Pirkimo grįžimas, pirkimo grąža

**Grįžti** (`Return`, navigation context):
Navigacijos veiksmas, perkeliantis naudotoją į ankstesnį ekraną arba veiksmą.
_Vengti_: Grąžinimas, grąžinti

**Atsargos** (`Stock`):
Įmonės turimos apskaitomos prekės ir jų kiekiai.
_Vengti_: Sandėlis, prekės

**Sandėlis** (`Warehouse`):
Fizinė arba loginė vieta, kurioje apskaitomos atsargos.
_Vengti_: Atsargos, saugykla

**Atsargų operacija** (`Stock Entry`):
Dokumentas, kuriuo registruojamas atsargų kiekio arba vietos pasikeitimas.
_Vengti_: Sandėlio įrašas, atsargų įrašas

**Atsargų registras** (`Stock Ledger`):
Chronologinis atsargų operacijų ir jų poveikio kiekiui bei vertei registras.
_Vengti_: Sandėlio knyga, atsargų žurnalas

**Atsargų likutis** (`Stock Balance`):
Tam tikru metu apskaitytas prekės kiekis ir vertė sandėlyje.
_Vengti_: Sandėlio balansas, prekių balansas

**Didžioji knyga** (`General Ledger`):
Pagrindinis apskaitos registras, kuriame operacijos sugrupuotos pagal apskaitos sąskaitas.
_Vengti_: Bendrasis registras, pagrindinė knyga

**Didžiosios knygos įrašas** (`GL Entry`):
Vienos operacijos debeto arba kredito įrašas Didžiojoje knygoje.
_Vengti_: GL įrašas, bendrojo registro įrašas

**Bendrojo žurnalo įrašas** (`Journal Entry`):
Apskaitos dokumentas, kuriuo užregistruojama vienos ar daugiau sąskaitų debeto ir kredito korespondencija.
_Vengti_: Žurnalo įrašas, buhalterinė pažyma

**Sąnaudų centras** (`Cost Center`):
Apskaitos dimensija, kuriai priskiriamos ir pagal kurią analizuojamos organizacijos pajamos bei sąnaudos.
_Vengti_: Kaštų centras, išlaidų centras

**Sąskaita faktūra** (`Invoice`):
Atsiskaitymo dokumentas, kuriame nurodytos parduotos arba įsigytos prekės, paslaugos ir mokėtina suma.
_Vengti_: Sąskaita, PVM sąskaita faktūra

**Pardavimo sąskaita faktūra** (`Sales Invoice`):
Klientui išrašyta sąskaita faktūra už parduotas prekes arba paslaugas.
_Vengti_: Pardavimų sąskaita, kliento sąskaita

**Pirkimo sąskaita faktūra** (`Purchase Invoice`):
Iš tiekėjo gauta sąskaita faktūra už įsigytas prekes arba paslaugas.
_Vengti_: Pirkimų sąskaita, tiekėjo sąskaita

**Komercinis pasiūlymas** (`Quotation`):
Klientui pateiktas pasiūlymas su prekėmis, kainomis, terminais ir kitomis pardavimo sąlygomis.
_Vengti_: Pasiūlymas, kainos pasiūlymas, citata

**Pristatymo dokumentas** (`Delivery Note`):
ERPNext dokumentas, kuriuo registruojamas prekių perdavimas klientui.
_Vengti_: Važtaraštis, pristatymo važtaraštis

**Gaminio specifikacija** (`Bill of Materials`):
Gaminamą prekę sudarančių medžiagų, tarpinių gaminių ir gamybos operacijų aprašas.
_Vengti_: Medžiagų sąrašas, medžiagų žiniaraštis

**BOM**:
Patvirtinta techninė santrumpa, paliekama nepakeista, kai šaltinio frazėje nėra pilno `Bill of Materials` termino.
_Vengti_: GS, GSpec

**Potencialus klientas** (`Lead`):
Asmuo arba organizacija, kuri gali tapti klientu, bet dar nėra įvertinta kaip konkretus galimas pardavimas.
_Vengti_: Lidas, vedlys, susidomėjęs

**Pardavimo galimybė** (`Opportunity`):
Įvertintas galimas pardavimas potencialiam arba esamam klientui.
_Vengti_: Proga, oportunitetas, verslo galimybė

**Juodraštis** (`Draft`):
Išsaugotas dokumentas, kuris dar nėra galutinai patvirtintas.
_Vengti_: Projektas, ruošinys

**Patvirtinti** (`Submit`):
Galutinai užregistruoti dokumentą ir pritaikyti jo poveikį sistemoje.
_Vengti_: Pateikti, siųsti

**Patvirtintas** (`Submitted`):
Dokumento būsena po sėkmingo patvirtinimo.
_Vengti_: Pateiktas, nusiųstas

**Pritarti** (`Approve`):
Darbo eigos sprendimu leisti dokumentui ar prašymui pereiti į kitą būseną.
_Vengti_: Patvirtinti, pateikti

**Patvirtinti pasirinkimą** (`Confirm`, dialog context):
Dialoge galutinai patvirtinti naudotojo pasirinktą veiksmą.
_Vengti_: Pritarti, pateikti

**Atšaukti** (`Cancel`):
Panaikinti patvirtinto dokumento poveikį išsaugant jo istoriją.
_Vengti_: Nutraukti, anuliuoti, ištrinti

**Atšauktas** (`Cancelled`):
Dokumento būsena po sėkmingo jo poveikio panaikinimo.
_Vengti_: Nutrauktas, anuliuotas, ištrintas

**Taisyti patvirtintą dokumentą** (`Amend`):
Sukurti naują atšaukto dokumento versiją, kurią galima pataisyti ir patvirtinti iš naujo.
_Vengti_: Papildyti, pakeisti, redaguoti

**Dokumento tipas** (`DocType`):
Frappe apibrėžtas dokumento duomenų, elgsenos ir teisių modelis.
_Vengti_: Dokumento rūšis, doctype

**Įrašas** (`Record`):
Viena konkreti dokumento tipo duomenų reikšmių visuma.
_Vengti_: Dokumento tipas, eilutė

**Darbo sritis** (`Workspace`):
Frappe navigacijos puslapis, jungiantis vienos veiklos nuorodas, rodiklius ir ataskaitas.
_Vengti_: Darbastalis, darbo vieta

**Suvestinė** (`Dashboard`):
Viename vaizde pateiktas susijusių rodiklių, diagramų ir būsenų rinkinys.
_Vengti_: Valdymo skydas, prietaisų skydelis

**Ataskaita** (`Report`):
Pagal pasirinktus kriterijus suformuotas sistemos duomenų vaizdas.
_Vengti_: Raportas, pranešimas

### Translation Style

**Official Polite Style**:
Neutralus dalykinis tonas, kuriame mygtukai ir meniu rašomi bendratimi, pavyzdžiui, „Išsaugoti“, o instrukcijos ir klausimai formuluojami mandagia daugiskaita, pavyzdžiui, „Pasirinkite“ ir „Ar norite tęsti?“.
_Vengti_: Kreipinys „tu“, pažodinis angliškas sakinys

**Sentence Case**:
Antraštės ir etiketės, kuriose didžiąja raide rašomas tik pirmasis žodis bei tikriniai vardai, nebent lietuvių kalbos taisyklė reikalauja kitaip.
_Vengti_: Kiekvieno Žodžio Rašymas Didžiąja Raide

**Lithuanian Quotation Marks**:
Lietuviškos kabutės „...“, naudojamos naudotojui rodomame tekste, kai kabutės nėra kodo, HTML arba parametro dalis.
_Vengti_: “...”, "..."

**Preserved Token**:
Šaltinio parametras, šablono išraiška, URL arba kitas vykdymui reikalingas ženklas, kurio rašyba ir kiekis vertime turi likti tikslūs, išsaugant HTML struktūrą, privalomus atributus ir reikšminius tarpus bei eilučių lūžius.
_Vengti_: Išverstas parametro vardas, pašalintas HTML elementas

**Glossary Lemma**:
Žodyne vienaskaitos vardininku pateikta termino forma, kuri sakinyje turi būti taisyklingai linksniuojama ir derinama.
_Vengti_: Nekaitomas žodyno termino įterpimas

## Relationships

- A **Translation Catalog** contains exactly one active entry for each **Active Translation Key**
- A **Release Inventory** freezes the **Active Translation Keys** for one released compatibility manifest
- A **Translation Key** consists of one **Source Phrase** and zero or one **Frappe Context** value
- A **Source Phrase** can have many **Source Locations**
- An approved **Translation Exception** counts toward **Translation Coverage** but is not an **English Fallback**
- A **Suspicious Translation** remains inherited until an **AI Translation** replaces it
- **Prekės kodas** identifies exactly one **Prekė** within an ERPNext site
- A **Klientas** can buy one or more **Prekės**
- A **Naudotojas** can be linked to a **Darbuotojas**, but they remain separate records
- An **Apskaitos sąskaita**, a **Paskyra**, and a **Banko sąskaita** are separate concepts even when the English source uses "Account"
- A **Pardavimo užsakymas**, a **Pirkimo užsakymas**, and a **Gamybos užsakymas** are separate documents
- English "Rate" maps to **Vieneto kaina**, **Valiutos kursas**, **Mokesčio tarifas**, **Valandinis įkainis**, or **Apskaitinė vieneto vertė** according to what is being measured
- English "Return" maps to **Pardavimo grąžinimas**, **Pirkimo grąžinimas**, or **Grįžti** according to whether it names a document or navigation
- **Atsargos** are held in one or more **Sandėliai** and changed by **Atsargų operacijos**
- A balanced and **Patvirtintas** **Bendrojo žurnalo įrašas** produces at least two **Didžiosios knygos įrašai** in the **Didžioji knyga**
- A **Didžiosios knygos įrašas** can be assigned to one **Sąnaudų centras**
- A **Pardavimo sąskaita faktūra** is issued to a **Klientas**, while a **Pirkimo sąskaita faktūra** is received from a **Tiekėjas**
- A **Komercinis pasiūlymas** can be accepted into a **Pardavimo užsakymas**
- A **Pardavimo užsakymas** can produce one or more **Pristatymo dokumentai**
- A **Gamybos užsakymas** uses one **Gaminio specifikacija**
- A **Potencialus klientas** can produce one or more **Pardavimo galimybės**, and a **Pardavimo galimybė** can produce a **Komercinis pasiūlymas**
- A **Juodraštis** becomes **Patvirtintas** after **Patvirtinti**; it must be **Atšauktas** before the user can **Taisyti patvirtintą dokumentą**

## Example Dialogue

> **Developer:** "The phrase `Item` appears in Stock, Sales, and Services. Should each module use a different word?"
> **Domain expert:** "No. They all refer to the ERPNext **Prekė** record. Use **Prekė** consistently, and use **Frappe Context** only when the product supplies a real runtime context."
>
> **Developer:** "`API` is unchanged in Lithuanian. Is that an **English Fallback**?"
> **Domain expert:** "No. It is a documented **Translation Exception**, so it still counts toward **Translation Coverage**."

## Flagged Ambiguities

- "Context" was used for both a runtime lookup qualifier and a source-code location; resolved: **Frappe Context** changes the **Translation Key**, while **Source Location** only informs translation choices.
- "Full user interface" could mean every English-looking string; resolved: **Translation Coverage** counts extracted **Active Translation Keys**, including keys exercised through standard portal, print, and email output; visible unmarked English is reported separately as an interface-quality defect, while user-created content, code, and logs remain outside scope; approved **Translation Exceptions** remain in the denominator and count as covered.
- "Complete" could mean source-text coverage without contexts; resolved: **Translation Coverage** is measured by **Translation Key**, not by unique English text.
- "Critical" could include any poor wording; resolved: a **Critical Translation Error** is defined by harmful action or blocked work, not by style alone.
- "Item" could mean only a physical product; resolved: **Prekė** is the short interface name for the broader ERPNext record, including services and non-stock uses.
- "Customer" could mean a buyer on one document or the long-lived ERP record; resolved: use **Klientas** for the ERPNext record and reserve "pirkėjas" for prose that explicitly describes the buyer's role.
- "Account" was used for ledger accounts, user identities, and bank accounts; resolved: use **Apskaitos sąskaita**, **Paskyra**, or **Banko sąskaita** according to the source meaning.
- One contextless **Translation Key** can occur in source locations with different meanings; resolved: source location never creates a new runtime key, and the known v16 `Account` collision uses the explicitly approved shared translation **Sąskaita** until upstream supplies a **Frappe Context**.
- "Order" was used for sales, purchasing, manufacturing, and list sorting; resolved: name each business document explicitly and use **Rikiavimo tvarka** only for sorting.
- "Rate" was used for price, currency conversion, tax, labor, and inventory valuation; resolved: use the Lithuanian term that names the measured quantity.
- "Return" was used for reversing business documents, navigating back, and source code; resolved: use **Grąžinimas** for business documents, **Grįžti** for navigation, and do not translate programming syntax.
- "Stock" and "Warehouse" were both used loosely for inventory; resolved: **Atsargos** are the quantities and values, while a **Sandėlis** is their physical or logical location.
- "Ledger" was used for both the accounting book and specialized operational histories; resolved: use **Didžioji knyga** only for the General Ledger and "registras" for stock or other ledgers.
- "Cost Center" had competing literal translations; resolved: use **Sąnaudų centras** and avoid "kaštų centras" and "išlaidų centras".
- "Invoice" and "Account" were both shortened to "sąskaita" in old translations; resolved: use **Sąskaita faktūra** for billing documents and the context-specific account terms for ledgers, users, and banks.
- "Quotation" was translated both literally and too broadly; resolved: use **Komercinis pasiūlymas** for the ERPNext sales document.
- "Delivery Note" was assumed to be a Lithuanian legal waybill; resolved: use **Pristatymo dokumentas** because the standard ERPNext record does not guarantee all waybill requirements.
- "BOM" could be expanded or translated inconsistently; resolved: use **Gaminio specifikacija** for the full term and preserve `BOM` when the source contains only the abbreviation.
- "Lead" and "Opportunity" were both used for prospective business; resolved: a **Potencialus klientas** is the person or organization, while a **Pardavimo galimybė** is the possible sale.
- "Submit" was treated as sending a document to someone; resolved: **Patvirtinti** means final registration in Frappe, while sending and sharing remain separate actions.
- "Submit", "Approve", and "Confirm" were treated as the same action; resolved: use **Patvirtinti** for Frappe submission, **Pritarti** for workflow approval, and **Patvirtinti pasirinkimą** for a confirmation dialog when the phrase permits it.
- "Amend" was treated as editing in place; resolved: **Taisyti patvirtintą dokumentą** creates a new version after cancellation.
- "User" and "Employee" were treated as the same person; resolved: **Naudotojas** represents access to the system, while **Darbuotojas** represents an employment relationship.
- Glossary entries could be copied without Lithuanian inflection; resolved: every Lithuanian entry is a **Glossary Lemma** that must be inflected and grammatically agreed in natural-language phrases.
