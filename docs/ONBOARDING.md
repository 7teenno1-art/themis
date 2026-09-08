# First run with Themiz

<p align="center"><img src="assets/pantheon/workflow/01-intake.png" alt="Themiz intake illustration, not an application screenshot" width="100%"></p>

This path uses macOS, Python 3.11 or newer, and Xcode Command Line Tools. Apple Vision recognizes scans only on a Mac. A full Windows or Linux installation has not been verified. Access to your chosen agent and its subscription limits are configured separately.

If you prefer a conversation, open the [step-by-step onboarding](ONBOARDING-CHAT.md) in Codex CLI or Claude Code. Both clients are equal entry paths. Ask about disk changes before installing.

## Step 1: Check your machine

```bash
sw_vers -productVersion
python3 --version
xcode-select -p
```

The first command shows your installed macOS version, the second Python 3.11 or newer, and the third a developer-tools path. Audio transcription also needs ffmpeg and a local Whisper model. If anything is missing, the installer names it in the final report. A failed or timed-out check has not passed.

## Step 2: Get the project

```bash
git clone https://github.com/zarubinvibe/themiz.git
cd themiz
```

The first command creates a `themiz` folder. The second moves your terminal into it. Without Git, download the [ZIP](https://github.com/zarubinvibe/themiz/archive/refs/heads/main.zip), extract it, and open the extracted folder in your terminal.

## Step 3: Install and activate the environment

```bash
bash install.sh
. .venv/bin/activate
```

The installer puts Python packages in `.venv` and prepares the font, local recognition tools, and working directories. It prints a report when finished. After the second command, your prompt will usually start with `(.venv)`.

## Step 4: Choose a client

Codex CLI:

```bash
codex
```

Ask it to configure Themiz with the `themiz-setup` skill.

Claude Code:

```bash
claude
```

Then run `/themiz-setup`. In either client, the agent should read the project rules, ask about your practice, and explain required access before working with client material. Both paths configure the same project.

## Step 5: Test the boundaries with a safe example

Start with a copy of a file that contains no sensitive data. Text extraction and OCR run locally. Fragments sent to the agent are processed by your chosen model provider. Before using a real case, ask the agent to state that boundary in plain language. Do not change source material in `00_intake/` after intake.

The public clone does not contain a populated legal corpus. Allowed sources and required documents are configured separately. A failed download or unconfirmed version remains an open issue. Text on disk does not prove that a legal provision is current.

## Step 6: Read the case status

First create the case with an agent. Replace `cases/client/case` with the actual path of an existing case folder. This command gives a summary of that specific case, not a general runtime overview.

```bash
python3 scripts/themiz_status.py cases/client/case --brief
```

Check the fact map against the originals: amounts, case numbers, and quotations need verification. Narrow tasks use FAST. Complex tasks use FULL with separate readers and review. A role that did not write the draft reviews it. You decide whether to sign and file.

## Step 7: Open the browser panel if you want it

```bash
.venv/bin/python cockpit/app.py
```

Open `http://127.0.0.1:8800`. You will see the local status panel. It is optional, and its agent buttons currently call Claude Code only. Use Codex CLI for Codex tasks.

## Step 8: Update an existing Git clone

In Codex, ask to update Themiz with the `themiz-update` skill. In Claude Code, run `/themiz-update`. This update path requires a Git clone. For a ZIP installation, make a fresh Git clone; after checking it, move local data with an agent and do not overwrite source files. Inspect the proposed changes and your working tree first. Updating the program and verifying legal versions remain separate operations.

## Feedback and contributions

Useful? [Give the project a star](https://github.com/zarubinvibe/themiz). Found a bug? [Open an issue](https://github.com/zarubinvibe/themiz/issues) with the command, error message, and a synthetic example. Do not publish client material.

To contribute a fix: fork the repository, create a branch, commit the change, push the branch, then open a Pull Request. Do not push changes directly to `main`.
