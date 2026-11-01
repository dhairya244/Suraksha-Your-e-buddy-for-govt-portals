# Hand-walk: Income Certificate (Sugam) — Digital Gujarat

Walked by: <name> · Date: <YYYY-MM-DD> · OS / browser: <macOS Chrome 156> · Zoom: 100%
**Dummy data only** — never real Aadhaar / phone / names. Stop before Submit.

How to fill this in (scripts/build_form_map.py reads it):
- One `## Page:` section per screen, in the order you meet them. Copy the page title exactly.
- One `### Field:` section per field, in on-screen order. The text after `Field:` is the label
  **exactly as shown** (English UI). Copy the Gujarati label exactly from the Gujarati UI.
- Keep the `- key: value` lines; leave a value empty if it doesn't apply. Delete the example blocks.
- `errors seen`: trigger errors on purpose with dummy data and paste the exact text, separated by ` ; `.
- Take a screenshot of every page at 100% and 125% → `demo/screens/<mac|win>/<page_id>_<zoom>.png`.

---

## Page: <Page title exactly as shown, e.g. Applicant Details>
- page_id: <snake_case, e.g. applicant_details>
- identifying phrases: <2–3 phrases visible on this page, separated by ;> 
- login page: no

### Field: <Label exactly as shown, e.g. Applicant Name>
- field_id: <optional snake_case; default = from label>
- gujarati label: <copied exactly from the Gujarati UI>
- type: <text | number | date | dropdown | radio | checkbox | file | otp | password>
- required: <yes | no>  (has * ?)
- options: <dropdown/radio choices exactly, separated by ;>
- format hint: <placeholder / format shown, e.g. DD/MM/YYYY>
- upload rules: <file fields: allowed formats and max size exactly as shown>
- errors seen: <"exact error text" ; "another error">
- notes: <anything else: what it's for, where the user finds the value, which ask-back it depends on>

### Field: <Next label>
- gujarati label:
- type:
- required:
- options:
- format hint:
- upload rules:
- errors seen:
- notes:

---

## Page: <Next page title>
- page_id:
- identifying phrases:
- login page: no

### Field: <Label>
- gujarati label:
- type:
- required:
- options:
- format hint:
- upload rules:
- errors seen:
- notes:

---

## After submit (what the portal shows; do NOT actually submit with real data)
- next steps:
- timeline:
- fee:
- tracking:
