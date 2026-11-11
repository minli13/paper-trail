# Paper Trail FAQ Agent

A small browser-based FAQ assistant that searches local documents, asks an
OpenRouter model to answer using matching passages, and reports the source
files. It can also preview and save an answer as a Markdown note after
confirmation.

## Requirements

- Python 3.10 or later
- An OpenRouter API key
- Internet access when asking questions (the documents themselves stay local)
- Packages in `requirements.txt`

Install the web server dependencies from the project directory:

```powershell
python -m pip install -r requirements.txt
```

## Set up the API key

The agent reads the key from the `OPEN_ROUTER_API_KEY` environment variable.
Do not put the key in this README or commit it to source control.

In PowerShell, set it for the current terminal session:

```powershell
$env:OPEN_ROUTER_API_KEY = "your-openrouter-key"
```

To store it for future Windows terminal sessions, use `setx` and open a new
terminal afterward:

```powershell
setx OPEN_ROUTER_API_KEY "your-openrouter-key"
```

## Run

From the project directory, start the FastAPI server:

```powershell
python -m uvicorn web_app:app --reload
```

Open <http://127.0.0.1:8000> in your browser. The browser UI replaces the
interactive CLI as the primary interface. The old CLI remains available with
`python .\faq_agent.py`.

By default, the web app:

- Reads `.md`, `.rst`, and `.txt` files from `.\documents`; upload documents
  from the sidebar (UTF-8 text, up to 5 MB per file).
- Uses the model configured in `faq_agent.py` (override with
  `OPENROUTER_MODEL`).
- Saves confirmed notes to `.\notes`.

Override the document folder, notes folder, or model with environment variables:

```powershell
$env:FAQ_DOCUMENTS_DIR = ".\my-docs"
$env:FAQ_NOTES_DIR = ".\my-notes"
$env:OPENROUTER_MODEL = "liquid/lfm-3-1.7b:free"
python -m uvicorn web_app:app --reload
```

The browser displays the answer, numbered source files, retrieved passages,
and any citation-number warnings. Use **Save answer as a note** to preview a
note; it is written only after selecting **Save note** in the confirmation
dialog.
Uploaded documents are stored in the configured documents folder. After a note
is saved, use the **Download note** link in the confirmation dialog to download
the Markdown file.

## Add your own documents

Place text-based `.md`, `.rst`, or `.txt` files in `documents` (or the folder
passed with `--documents`). Subdirectories are searched too. The sample
documents in that folder are fictional and are included for trying out the
agent. The repository's `.gitignore` excludes other files in `documents/`,
including your uploaded documents, and excludes generated notes. This helps
prevent publishing private document content when pushing the project.

The ignore rules also cover environment files and common credential files.
They do not remove secrets that were already committed or staged; check your
Git status and history before publishing, and rotate any credential that may
have been exposed.

Search currently scores paragraphs by matching query terms. Answers are sent
only the matching passages, with instructions to rely on those passages and
cite the supporting source files together in one bracket when multiple files
support the claim. If no passages match, the agent reports that it could not find
relevant evidence rather than asking the model to answer without sources.

## DSL commands

The agent asks the model for a small action plan using these commands:

```text
SEARCH(query[, max_results])
ANSWER(question, passages)
SAVE_NOTE(title, content, sources)
LIST_DOCS()
```

- `SEARCH` returns matching local passages with source filenames. `max_results`
  is optional and defaults to 5.
- `ANSWER` asks the model to answer using the supplied passages. In an FAQ plan,
  use the special `$SEARCH_RESULTS` value for `passages`; the agent replaces it
  with the results from the preceding `SEARCH`.
- `SAVE_NOTE` previews a note and saves it only after the user confirms. The
  terminal offers to save each answer after displaying it.
- `LIST_DOCS` lists available supported documents.

The FAQ agent parses the model output as command syntax, checks that commands
are supported and arguments are literal values, and rejects executable Python
and argument unpacking. Supported named arguments are normalized to the
commands' positional order. A valid FAQ plan must include `SEARCH` and
`ANSWER`. The agent then executes the plan in order, passes search results to
`ANSWER`, checks that answer citation numbers map to the supplied source files,
and shows both the answer and retrieved passages. The DSL executor performs the
local search and note writing; note writing requires an explicit confirmation.

The HTTP interface provides `GET /api/documents`, `POST /api/ask`, and
`POST /api/notes`. The browser only communicates with this same-origin API;
the OpenRouter key remains on the server and is read from
`OPEN_ROUTER_API_KEY`.

## Run the tests

```powershell
python -m unittest discover -s tests -v
```
