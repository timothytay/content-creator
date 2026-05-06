# How to Use the B-Roll Pipeline

This tool takes your b-roll footage and a voiceover recording, and automatically builds a video timeline ready to edit in DaVinci Resolve. You do the creative work — it handles the matching, timing, and assembly.

---

## What you'll need before starting

- A Mac (these instructions are written for macOS)
- Your b-roll footage as `.mp4` files
- A voiceover recording as an `.mp3` file
- An OpenAI account (to pay for the AI that analyses your footage)
- DaVinci Resolve installed (free version works fine)
- About 20 minutes to get set up the first time

---

## One-time setup

### Step 1 — Install Python

1. Go to [python.org/downloads](https://python.org/downloads)
2. Click the big yellow **Download Python** button
3. Open the downloaded file and follow the installer
4. When it finishes, open the **Terminal** app (press `Cmd + Space`, type `Terminal`, press Enter)
5. Type the following and press Enter — if you see a version number, Python is installed:
   ```
   python3 --version
   ```

### Step 2 — Install FFmpeg

FFmpeg is a free tool that handles all the video processing.

1. Install Homebrew first (a Mac package manager) by pasting this into Terminal and pressing Enter:
   ```
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
   It will ask for your Mac password. Type it (nothing will appear on screen — that's normal) and press Enter.

2. Once Homebrew finishes, install FFmpeg:
   ```
   brew install ffmpeg
   ```
   This may take a few minutes.

### Step 3 — Get an OpenAI API key

1. Go to [platform.openai.com](https://platform.openai.com) and sign in or create an account
2. Click your profile icon (top right) → **API keys**
3. Click **Create new secret key**, give it a name, and click **Create**
4. Copy the key (it starts with `sk-`) — you won't be able to see it again, so save it somewhere safe

### Step 4 — Open the project folder in Terminal

1. Open Terminal
2. Type `cd ` (with a space after it), then drag the **iosif_workflow** folder from Finder into the Terminal window — the path will fill in automatically
3. Press Enter

You should now see the folder name at the end of your Terminal prompt.

### Step 5 — Install the Python packages

Paste this into Terminal and press Enter:
```
pip3 install -r requirements.txt
```
Wait for it to finish (may take a minute or two).

### Step 6 — Set your OpenAI API key

Paste this into Terminal, replacing `YOUR_KEY_HERE` with the key you copied in Step 3:
```
export OPENAI_API_KEY=YOUR_KEY_HERE
```

> **Note:** You'll need to run this command every time you open a new Terminal window. To avoid that, add it to your shell profile — ask a technical person to help with this if needed.

---

## Using the tool

### Part 1 — Ingest your b-roll footage (do this once per batch of footage)

This step analyses your footage so the tool understands what's in each clip. You only need to do it once per file — the tool remembers everything.

In Terminal, run:
```
python3 main.py ingest /path/to/your/footage/
```

Replace `/path/to/your/footage/` with the path to your b-roll folder (you can drag the folder into Terminal to get its path).

You can also add individual files:
```
python3 main.py ingest footage/clip1.mp4 footage/clip2.mp4
```

**What's happening:** The tool is splitting each video into 4-second clips, taking snapshots from each clip, and sending them to GPT-4o to understand what's visually in each one. This costs a small amount of OpenAI credit — typically a few cents per minute of footage.

When it finishes you'll see a summary like:
```
✓ Done. Library: 847/847 clips tagged across 3 sources.
```

### Part 2 — Produce a video from a voiceover

Run:
```
python3 main.py produce voiceover.mp3 --output my_video.mp4
```

Replace `voiceover.mp3` with the path to your voiceover file, and `my_video.mp4` with whatever you want the output called.

**What's happening:**
1. Your voiceover is sped up slightly (1.1×) — this is intentional to tighten the pacing
2. It's transcribed word-by-word with timestamps
3. GPT-4o splits the transcript into topics
4. The best-matching b-roll clips are selected and scheduled for each topic
5. Everything is assembled and exported

When it finishes, you'll have three files next to your voiceover:

| File | What it is |
|---|---|
| `my_video.mp4` | The assembled b-roll video (no audio) — for reference |
| `my_video.fcpxml` | **The file you import into DaVinci Resolve** |
| `my_video.schedule.json` | A text breakdown of the timeline (for reference) |
| `voiceover_1.1x.mp3` | The sped-up voiceover — already embedded in the timeline |

---

## Editing in DaVinci Resolve

1. Open DaVinci Resolve
2. Create a new project (or open an existing one)
3. Go to **File → Import → Timeline…**
4. Select `my_video.fcpxml`
5. Click **OK** on any import settings dialog

You'll now have a timeline with:
- **Video track** — b-roll clips already placed and timed to your voiceover
- **Audio track** — your sped-up voiceover, pre-synced
- **Gap placeholders** — labeled `FACE CAM – [topic]`, one for each section where your face cam footage goes

Simply drag your face cam recordings onto the gap placeholders, do your colour grade, and export.

---

## Checking your library

To see how many clips are in your library at any time:
```
python3 main.py stats
```

---

## Troubleshooting

**"command not found: python3"**
Python isn't installed, or the Terminal window is new and needs the API key set again. Re-run Step 1.

**"OPENAI_API_KEY is not set" or authentication errors**
You need to set your API key. Run the `export OPENAI_API_KEY=...` command from Step 6 again in the current Terminal window.

**"No MP4 files found"**
The path you gave doesn't point to your footage folder. Try dragging the folder directly into Terminal to get the correct path.

**"No tagged clips in library. Run ingest first."**
You need to ingest your b-roll footage before producing a video. Run the ingest command from Part 1.

**FFmpeg errors during ingest or produce**
FFmpeg may not be installed correctly. Run `ffmpeg -version` in Terminal — if it says "command not found", re-run `brew install ffmpeg`.

**The timeline looks wrong in DaVinci Resolve**
Make sure you're importing via **File → Import → Timeline** (not dragging the file in). Dragging imports it as a media clip, not a timeline.
