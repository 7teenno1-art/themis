# Themiz: install by conversation

<p align="center"><img src="assets/pantheon/emblem.png" alt="The Themiz emblem in white marble: scales and a lowered sword against a classical column" width="100%"></p>

<!-- owner-greeting:start -->

Hello. I am Fil, a lawyer in Kazan, and I wrote Themiz for myself.

The mechanics of a case ate my evenings: two hundred pages of scans, checking dates, verifying
citations. Necessary work, but not the work anyone joins the profession for. Themiz took the
mechanics and left me the decisions.

I will walk you through the install step by step. Before each step I say what happens to your
machine, and I stop where the choice is yours. If something breaks, open an Issue: I read them.

- Filipp Zarubin

<!-- owner-greeting:end -->

## Step 1: I look at what your machine already has

**What I do:** check the macOS version, Python 3.11 or newer, Xcode command line tools and
whether an agent CLI is installed. I install nothing here, I only look.

**Why:** scan recognition runs through Apple Vision, which lives only on a Mac. Better to learn
that now than halfway through.

**What changes on disk:** nothing. This is reading.

**What you get:** a short list of what is missing, with one command per item.

**Fork:** no Mac at hand. Themiz still runs elsewhere, but it cannot read scans: recognition
drops out and everything else stays. Tell me whether we go that way or you come back with a Mac.

## Step 2: I install the dependencies

**What I do:** run `bash install.sh`. The installer moves in seven steps: Python packages, the
PT Serif font for court documents, Apple Vision recognition, ffmpeg for transcribing recordings,
script permissions, working directories and the agent CLI.

**Why:** this is the body of Themiz. Without the packages no document is assembled, without the
font it will not look like a document, without ffmpeg a hearing recording stays unreadable.

**What changes on disk:** Python packages into the project environment, the font into the system
font directory, working folders `cases/_logs`, `cases/_assets`, `knowledge` and an inbox folder
on your desktop. The installer asks permission before every step.

**What you get:** a working install and a report on each of the seven steps.

## Step 3: we choose how you will work

**What I do:** show four doors into the same house: `claude`, `codex`, `code .` for an editor,
and `python3 cockpit/app.py` for the browser panel with no agent at all.

**Why:** the agents and the rules live inside the project, not inside one tool. You change the
tool, the house stays the same.

**What changes on disk:** nothing. Choosing a door rewrites nothing.

**What you get:** your familiar environment with the Themiz rules already loaded.

**Fork:** you do not want an agent near case material at all. Then take the browser panel: it
shows the state of your cases and never calls a model.

## Step 4: we open the first case

**What I do:** ask for the client and the matter, check conflict of interest against the
register of past cases, create the folder layout and move material in from the inbox.

**Why:** conflict of interest is checked before the work, not after. If we once acted against
this person, I stop and say so.

**What changes on disk:** a new folder `cases/{client}/{matter}` with directories for material,
drafts and finished documents. Your sources are copied, the originals stay where they are.

**What you get:** an opened case and an honest answer on conflict of interest.

## Step 5: I run the protocol

**What I do:** carry the case through five steps. A map of the material, a search for practice
both for you and against you, an agreed position, document drafts, and a review of every
document by a second agent.

**Why:** whoever wrote the document does not accept it. The reviewer reads the disk, not the
author's report, and the verdict is written by an instrument rather than by the reviewer.

**What changes on disk:** the case map, the practice file, the position file, the drafts and the
verdict journal inside the case folder. Nothing leaves the case.

**What you get:** a package of documents, each with a verdict and a trace of the review. The last
word stays yours: signing or rewriting is your call.

<p align="center"><img src="assets/pantheon/takt-en.png" alt="The Themiz tact as one wide marble scene: seven named steps from Intake to Hearing, tied by a blue thread" width="100%"></p>

## Step 6: we make sure it is all alive

**What I do:** run the instrument self-checks and show the state of the case in one command.

**Why:** a promise either holds or it does not. Checking is cheaper than believing.

**What changes on disk:** nothing beyond the run log.

**What you get:** a list of instruments with their exit codes and a case summary: which step is
closed, which is next, where material is missing.

```bash
./bin/vision-doc --selftest
python3 scripts/themiz_status.py cases/{client}/{matter} --brief
```
