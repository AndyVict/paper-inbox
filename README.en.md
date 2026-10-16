# Paper Inbox

**Read your X-MOL / Stork literature alerts as paper briefs on your phone.**

Merge duplicate papers, add Chinese titles and abstracts, look up journal metrics in the bundled snapshot, and send each paper through WxPusher App. Each message contains the paper, its journal, an abstract and a link to read more.

[简体中文](README.md) · [Complete message examples](examples/demo-report.md) · [Setup](docs/SETUP.md) · [Agent Skill](docs/SKILL.md)

[![CI](https://github.com/AndyVict/paper-inbox/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/AndyVict/paper-inbox/actions/workflows/ci.yml)

## Less sorting, more reading

- Collect X-MOL and Stork subscriptions from your mailbox on a weekly schedule.
- Merge duplicates across providers and runs, preserving cached translations and delivery history.
- Read one message per paper: Chinese and English titles, journal, JIF/JCI quartiles, Chinese abstract and paper link.
- Resume from SQLite state and caches. Uncertain delivery attempts remain available for review.

Chinese translation uses NiuTrans. A local journal snapshot ships with the project: **22,643 journals, 32,215 category records and 254 subjects**. Each stage can be enabled separately, and English previews work without translation.

## Message preview

![Complete local message preview with titles, journal metrics, a Chinese abstract and a paper link](docs/assets/demo.png)

*A local message preview using the production delivery template. Papers are CC BY 4.0; Chinese text uses existing machine translations. See the [examples and attribution](examples/demo-report.md).*

## See the result first

Open the [complete message examples](examples/demo-report.md) directly on GitHub, or generate the same HTML locally:

```console
git clone https://github.com/AndyVict/paper-inbox.git
cd paper-inbox
python -X utf8 scripts/showcase.py
```

Open `data/showcase/report.html`. It contains two existing message examples with full abstracts and all seven fields. It requires no credentials, makes no network requests and sends no notifications.

For a separate parser demonstration, run `python -X utf8 scripts/demo.py` and open `data/demo/report.html`: **2 synthetic emails → 4 candidates → 3 unique papers**. Demonstration data stays separate from the live queue.

## Look up a journal offline

From a source checkout, no account, API key or external data connection is needed:

```console
python -X utf8 scripts/journals.py --issn 1664-462X
python -X utf8 scripts/journals.py "Nature" --limit 5
```

After installation use `paper-inbox journals`; add `--json` for structured output. The shipped snapshot contains 2025 metrics with a 2026 release year. The maintainer individually downloaded the records and confirmed permission to publish; source/version and official completeness have not been independently verified. See the [dataset notes](docs/JCR.md).

## Start using it

Requires **Python 3.11+**. The runtime uses the Python standard library. From the repository root, on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
.\.venv\Scripts\paper-inbox.exe sync --check-config
.\.venv\Scripts\paper-inbox.exe sync --fetch --max-mails 20
```

Edit the `IMAP_*` fields in your local `.env` before running the last two commands. Existing configuration is preserved. Read the results in `data/outbox/`; mail remains unread and is never moved or deleted.

On macOS / Linux:

```console
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/paper-inbox --help
```

Create `.env` from `.env.example` for a fresh setup and use `.venv/bin/paper-inbox` for subsequent commands. The detailed setup guide is currently Chinese.

After configuring NiuTrans, generate Chinese previews:

```console
paper-inbox weekly --fetch --stages sync,abstracts,translation,jcr,delivery --language zh
```

Use the executable inside your virtual environment. Output goes to `data/send-preview/`; this command does not send notifications. For a single test notification, receipt confirmation and scheduling, follow the [complete setup route](docs/SETUP.md).

| Feature | What you need |
| --- | --- |
| Email collection | TLS IMAP and password/app-password authentication; 163 has live validation |
| Public abstracts | Access to PubMed / Europe PMC / Crossref / OpenAlex |
| Chinese translation | A configured NiuTrans `cloud` or `neu` account with available quota |
| Journal metrics | Use the bundled snapshot or [import your own](docs/JCR.md) |
| Phone notifications | WxPusher App, an app token and recipient UID |

## Support and defaults

The primary delivery target is **WxPusher App**. WeChat delivery depends on provider binding and activation requirements. The manual end-to-end workflow has been used; the first scheduled weekly observation is still pending. Windows Task Scheduler scripts are included; other system schedulers are not validated.

Other IMAP providers have mocked coverage only. OAuth2 is not implemented. Missing abstracts remain missing. The bundled snapshot is frozen, with original missing metrics and three invalid source identifiers retained. It is not presented as a current or independently verified official database.

`weekly` defaults to English previews. Chinese requires the `translation` stage and `--language zh`. Retrieval requires `--fetch`; delivery requires `--send`. Confirm receipt of one test notification before enabling weekly delivery. See [validation records](docs/RELEASE_STATUS.md).

## Agent assistance and contributions

Install the Python tool and the [paper-inbox Skill](skills/paper-inbox/SKILL.md) separately. The [installation guide](docs/SKILL.md) explains how to use the Skill for setup, previews and troubleshooting.

Contributions with synthetic parser samples and usability feedback are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and [PROVENANCE.md](PROVENANCE.md).

```console
python -X utf8 -m unittest discover -s tests -q
python -X utf8 scripts/check_release.py
```

Project code and original documentation use [MIT](LICENSE). Showcase papers and translations retain their stated third-party licenses.
