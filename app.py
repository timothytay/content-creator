"""
app.py — Web UI for the b-roll pipeline.
Run with: python3 app.py
"""

import json
import queue
import shutil
import sys
import threading
from pathlib import Path

import gradio as gr

import config
import library

SETTINGS_FILE = Path("ui_settings.json")


# ── Settings persistence ───────────────────────────────────────────────────────

def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_settings(key: str, value):
    s = _load_settings()
    s[key] = value
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


# ── Stdout capture ─────────────────────────────────────────────────────────────

class _LineCapture:
    """Redirect sys.stdout into a queue line by line."""
    def __init__(self, q: queue.Queue):
        self._q   = q
        self._buf = ""

    def write(self, text: str):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._q.put(line + "\n")

    def flush(self):
        if self._buf:
            self._q.put(self._buf)
            self._buf = ""


def _stream(fn, *args):
    """Run fn(*args) in a thread and yield its stdout output incrementally."""
    q: queue.Queue = queue.Queue()

    def _target():
        old = sys.stdout
        sys.stdout = _LineCapture(q)
        try:
            fn(*args)
        except Exception:
            import traceback
            sys.stdout = old
            q.put(f"\n❌ {traceback.format_exc()}")
        finally:
            sys.stdout = old
            q.put(None)  # sentinel

    threading.Thread(target=_target, daemon=True).start()

    output = ""
    while True:
        try:
            line = q.get(timeout=0.2)
            if line is None:
                break
            output += line
            yield output
        except queue.Empty:
            yield output


# ── Helpers ────────────────────────────────────────────────────────────────────

def _file_path(f) -> str | None:
    if f is None:
        return None
    return f if isinstance(f, str) else getattr(f, "name", str(f))


def _stats_md() -> str:
    library.init_db()
    s = library.library_stats()
    pending = s["clips"] - s["tagged"]
    return (
        f"**Sources:** {s['sources']}   "
        f"**Clips:** {s['clips']} total · {s['tagged']} tagged"
        + (f" · {pending} pending" if pending else "")
    )


# ── Ingest handler ─────────────────────────────────────────────────────────────

def ingest(files, folder_path):
    from ingest import run_ingest

    paths = [_file_path(f) for f in (files or []) if f]
    if folder_path and folder_path.strip():
        paths.append(folder_path.strip())

    if not paths:
        yield "⚠ No files or folder provided.", _stats_md()
        return

    output = ""
    for chunk in _stream(run_ingest, paths):
        output = chunk
        yield output, _stats_md()

    yield output, _stats_md()


# ── Produce handler ────────────────────────────────────────────────────────────

def produce(voiceover_file, output_name, footage_folder, min_clips, max_clips, min_gap, max_gap):
    from ingest import run_ingest
    from produce import run_produce

    vo_path = _file_path(voiceover_file)
    if not vo_path:
        yield "⚠ No voiceover file provided.", None, None, None
        return

    min_clips, max_clips = int(min_clips), int(max_clips)
    min_gap,   max_gap   = int(min_gap),   int(max_gap)

    if min_clips > max_clips:
        yield "⚠ Min clips per group cannot exceed max clips.", None, None, None
        return
    if min_gap > max_gap:
        yield "⚠ Min gap cannot exceed max gap.", None, None, None
        return

    config.GROUP_MIN_CLIPS = min_clips
    config.GROUP_MAX_CLIPS = max_clips
    config.GAP_MIN_SEC     = min_gap
    config.GAP_MAX_SEC     = max_gap

    footage = footage_folder.strip() if footage_folder else ""
    if footage:
        _save_settings("footage_folder", footage)

    out = (output_name.strip() or "output")
    if not out.endswith(".mp4"):
        out += ".mp4"
    out_path = Path(out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    local_vo = out_path.parent / Path(vo_path).name
    if Path(vo_path).resolve() != local_vo:
        shutil.copy(vo_path, local_vo)

    output = ""

    # ── Phase 1: ingest new footage if a folder is set ────────────────────────
    if footage:
        output += f"── Auto-ingesting from: {footage}\n"
        yield output, None, None, None

        for chunk in _stream(run_ingest, [footage]):
            output = chunk
            yield output, None, None, None

        output += "\n"
        yield output, None, None, None

    # ── Phase 2: produce ──────────────────────────────────────────────────────
    results: dict = {}

    def _run():
        run_produce(str(local_vo), str(out_path))
        results["video"]    = str(out_path)    if out_path.exists() else None
        results["fcpxml"]   = str(out_path.with_suffix(".fcpxml"))
        results["schedule"] = str(out_path.with_suffix(".schedule.json"))

    for chunk in _stream(_run):
        output = output + chunk[len(output):]   # append only new text
        yield output, None, None, None

    yield (
        output,
        results.get("video"),
        results.get("fcpxml"),
        results.get("schedule"),
    )


# ── UI layout ──────────────────────────────────────────────────────────────────

def build_ui() -> gr.Blocks:
    settings = _load_settings()

    with gr.Blocks(title="B-Roll Pipeline") as app:

        gr.Markdown("# B-Roll Pipeline")

        with gr.Tabs():

            # ── Ingest ───────────────────────────────────────────────────────
            with gr.Tab("Ingest"):
                gr.Markdown(
                    "Add b-roll footage to the library. "
                    "Each file is hashed — re-running on the same file is safe."
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        ingest_files_input = gr.File(
                            label="Video files",
                            file_types=[".mp4", ".mov"],
                            file_count="multiple",
                        )
                        folder_input = gr.Textbox(
                            label="Or paste a folder path",
                            placeholder="/Volumes/SSD/footage/",
                        )
                        ingest_btn = gr.Button("Ingest", variant="primary", size="lg")

                    with gr.Column(scale=1):
                        stats_display = gr.Markdown(_stats_md())
                        ingest_log = gr.Textbox(
                            label="Log",
                            lines=18,
                            max_lines=40,
                            interactive=False,
                        )

                ingest_btn.click(
                    fn=ingest,
                    inputs=[ingest_files_input, folder_input],
                    outputs=[ingest_log, stats_display],
                )

            # ── Produce ──────────────────────────────────────────────────────
            with gr.Tab("Produce"):
                gr.Markdown(
                    "Optionally set a footage folder — new clips will be ingested "
                    "automatically each time you produce."
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        voiceover_input = gr.File(
                            label="Voiceover (.mp3)",
                            file_types=[".mp3"],
                        )
                        output_name = gr.Textbox(
                            label="Output filename",
                            value="output.mp4",
                        )
                        footage_folder_input = gr.Textbox(
                            label="Footage folder (auto-ingested before each produce)",
                            placeholder="/Volumes/SSD/footage/",
                            value=settings.get("footage_folder", ""),
                        )

                        gr.Markdown("### Clips per group")
                        with gr.Row():
                            min_clips = gr.Slider(
                                label="Min",
                                minimum=1, maximum=10, step=1,
                                value=config.GROUP_MIN_CLIPS,
                            )
                            max_clips = gr.Slider(
                                label="Max",
                                minimum=1, maximum=10, step=1,
                                value=config.GROUP_MAX_CLIPS,
                            )

                        gr.Markdown("### Blank gap length (seconds)")
                        with gr.Row():
                            min_gap = gr.Slider(
                                label="Min",
                                minimum=1, maximum=30, step=1,
                                value=config.GAP_MIN_SEC,
                            )
                            max_gap = gr.Slider(
                                label="Max",
                                minimum=1, maximum=30, step=1,
                                value=config.GAP_MAX_SEC,
                            )

                        produce_btn = gr.Button("Produce", variant="primary", size="lg")

                    with gr.Column(scale=1):
                        produce_log = gr.Textbox(
                            label="Log",
                            lines=18,
                            max_lines=40,
                            interactive=False,
                        )
                        gr.Markdown("### Output files")
                        out_video    = gr.File(label="Video (.mp4)")
                        out_fcpxml   = gr.File(label="DaVinci Resolve timeline (.fcpxml)")
                        out_schedule = gr.File(label="Schedule (.json)")

                produce_btn.click(
                    fn=produce,
                    inputs=[
                        voiceover_input, output_name, footage_folder_input,
                        min_clips, max_clips,
                        min_gap, max_gap,
                    ],
                    outputs=[produce_log, out_video, out_fcpxml, out_schedule],
                )

            # ── Library ──────────────────────────────────────────────────────
            with gr.Tab("Library"):
                gr.Markdown(
                    "Export your library to share with someone else, or import one you received. "
                    "The zip contains all clip files and the tag database — no re-ingesting needed."
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("### Export")
                        gr.Markdown(
                            "Creates a zip of your entire clip library. "
                            "Size depends on how much footage you've ingested."
                        )
                        export_btn = gr.Button("Export library", variant="primary")
                        export_log = gr.Textbox(label="Log", lines=6, interactive=False)
                        export_file = gr.File(label="Download")

                    with gr.Column(scale=1):
                        gr.Markdown("### Import")
                        gr.Markdown(
                            "Import a library zip from someone else. "
                            "New clips are merged in — your existing library is not overwritten."
                        )
                        import_file_input = gr.File(
                            label="Library zip",
                            file_types=[".zip"],
                        )
                        import_btn = gr.Button("Import library", variant="primary")
                        import_log = gr.Textbox(label="Log", lines=6, interactive=False)
                        import_stats = gr.Markdown(_stats_md())

                export_btn.click(
                    fn=export_library,
                    inputs=[],
                    outputs=[export_log, export_file],
                )
                import_btn.click(
                    fn=import_library,
                    inputs=[import_file_input],
                    outputs=[import_log, import_stats],
                )

    return app


# ── Entry point ────────────────────────────────────────────────────────────────

def export_library():
    dest = Path("broll_library_export.zip")
    output = ""
    for chunk in _stream(library.export_library, dest):
        output = chunk
        yield output, None

    yield output, str(dest) if dest.exists() else None


def import_library(zip_file):
    zip_path = _file_path(zip_file)
    if not zip_path:
        yield "⚠ No file provided.", _stats_md()
        return

    output = ""
    for chunk in _stream(library.import_library, Path(zip_path)):
        output = chunk
        yield output, _stats_md()

    yield output, _stats_md()


if __name__ == "__main__":
    library.init_db()
    library.migrate_to_relative_paths()
    build_ui().launch(inbrowser=True, theme=gr.themes.Soft())
