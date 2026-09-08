# Themiz: installation by conversation

<p align="center"><img src="assets/pantheon/emblem.png" alt="The Themiz emblem: marble scales and a sword by a column" width="100%"></p>

<!-- owner-greeting:start -->

Hello. I am Fil, a lawyer. I created Themiz for my practice.

I needed order in case materials, sources, and drafts. Themiz brings those stages together in one
workflow with a separate review. I make the legal-position and filing decisions.

Next, I will guide you through the installation. Before each action, I will tell you what will
change on your computer and what access is needed. If something does not work, write in Issues
what you ran and the response you received. Do not attach client materials there.

- Filipp Zarubin

<!-- owner-greeting:end -->

## Step 1: I check what is already on your machine

**What I do:** check the macOS version, whether Python 3.11 or newer is present, Xcode command
line tools, and an installed agent. I install nothing; I only inspect.

**Why:** scan recognition uses Apple Vision, which is available only on a Mac. It is better to
learn that now than halfway through installation.

**What changes on disk:** nothing. This is read-only.

**What you get:** a list of missing components and ways to install them.

**Fork:** if you do not have a Mac, Apple Vision is unavailable. A full installation on Windows
and Linux has not yet been verified. We can separately check text extraction on your system or
continue on a Mac.

## Step 2: I install the dependencies

**What I do:** run `bash install.sh`. The installer has seven steps: Python packages, the PT
Serif font for court documents, Apple Vision recognition, ffmpeg for transcribing recordings,
script permissions, working directories, and an agent CLI.

**Why:** these components extract text and assemble documents. Audio also needs ffmpeg and a
local Whisper model. Local operations do not require a paid model API.

**What changes on disk:** Python packages in the project's `.venv`, the font in the user font
directory, working folders `cases/_logs`, `cases/_assets`, `knowledge`, and an inbox folder on
the desktop. Before running it, I explain what the installation includes. Access to the selected
agent and its subscription are configured separately.

**What you get:** the installer report. A check error or timeout remains in that report; a
successful package installation alone does not confirm that every external service is ready.

## Step 3: we choose how to run it

**What I do:** activate the environment with `. .venv/bin/activate`. You choose one of two equal
clients: `codex` or `claude`. `code .` opens the project in an editor;
`.venv/bin/python cockpit/app.py` starts the local browser panel.

**Why:** the project includes rules and roles for Codex CLI and Claude Code. Each client needs
its own model access and available subscription limits.

**What changes on disk:** choosing a client does not change project settings.
The client and panel may create their own logs, working directories, and local state.

**What you get:** your chosen client for working with Themiz. I will check that it sees the
project rules.

**Fork:** if you do not permit a model provider to process case materials, we stay with local
extraction and viewing tools. Agent analysis is not local OCR. Task-launch buttons in the browser
panel currently call Claude Code; run Codex tasks from its CLI.

## Step 4: we open the first case

**What I do:** ask for the client and the case, check conflicts of interest against the register
of prior cases, create the folder layout, and move materials from the inbox.

**Why:** the local register helps spot matches before work begins. The program does not know
about cases absent from the register; you assess whether the conflict check is complete.

**What changes on disk:** a new `cases/{client}/{case}` folder with directories for materials,
drafts, and completed documents. Before moving files, we agree on their group; after intake, we
do not alter the source files in `00_intake/`.

**What you get:** a case card and the check result based on the available local register.

## Step 5: I run the protocol

**What I do:** prepare a map of the materials, check the availability of permitted sources,
search for arguments supporting and opposing your position, prepare a draft, and send it to a
separate reviewer. The scope of work and the council's composition depend on the case's
complexity.

**Why:** the person who wrote the document does not accept it. The reviewer reads the disk, not
the drafter's report, and an instrument records the verdict rather than the reviewer.

**What changes on disk:** a case map, a practice file, a position file, drafts, and a verdict log
inside the case folder. During agent analysis, the selected model provider processes the
fragments sent to it. We check permission for that processing before sending the materials.

**What you get:** a draft and its review result, or a specific reason to stop. A missing source is
not treated as verified. Before signing and filing, you review the document yourself.

<p align="center"><img src="assets/pantheon/takt-en.png" alt="Illustration of the Themiz workflow stages, not an application screenshot" width="100%"></p>

## Step 6: we make sure everything is working

**What I do:** run instrument self-checks and show the case status with one command.

**Why:** a promise either works or it does not. Checking is cheaper than believing.

**What changes on disk:** nothing except the run log.

**What you get:** a list of instruments with exit codes and a case summary: which step is closed,
which comes next, and where materials are missing.

If `bin/vision-doc` was built (requires macOS 26+), run `./bin/vision-doc --selftest`.
If only `vision-ocr` is available, check recognition with a safe test image;
that tool does not have a `--selftest` flag.

```bash
python3 scripts/themiz_status.py cases/{client}/{case} --brief
```
