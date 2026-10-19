"""
run.py - give a scene description, get a video.  (Python only: Gemini writes the animation code.)

Setup (once):
    pip install google-genai            (+ pip install manim   only if you use --engine manim)
    ffmpeg on PATH
    Node.js 18+ on PATH                 (only for --engine remotion; Remotion itself is installed
                                         automatically into build/remotion_project on the first run)
    .env file next to this script:      GEMINI_API_KEY=your_key

Usage:
    python run.py "Scene 1: nested boxes show a function calling itself. Scene 2: ..."
    python run.py --file scenes.txt --aspect 9:16 --quality medium
    python run.py --engine manim "Scene 1: ..."
    python run.py                       # no argument -> it asks you to type / paste the prompt

scenes.txt example:
    Topic: How Recursion Works
    Scene 1: Show a function calling itself using animated nested boxes.
    Scene 2: Visualize the call stack growing with each recursive call.
    Scene 3: Animate the stack unwinding and returning values.
"""
import argparse
import logging
import sys
from pathlib import Path

from scene_video import SceneError


def read_prompt(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.prompt:
        return " ".join(args.prompt).replace("\\n", "\n")
    print("Describe your scene(s). Finish with an empty line:")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip() and lines:
            break
        lines.append(line)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Scene description -> Gemini writes animation code -> MP4")
    ap.add_argument("prompt", nargs="*", help="scene description(s); 'Scene 1: ... Scene 2: ...' makes several scenes")
    ap.add_argument("--file", help="read the prompt from a text file")
    ap.add_argument("--engine", default="remotion", choices=["remotion", "manim"],
                    help="remotion (React/TSX, needs Node) or manim (pure Python). Default remotion")
    ap.add_argument("--aspect", default="16:9", help="16:9 | 9:16 | 1:1 | 4:5 (default 16:9)")
    ap.add_argument("--quality", default="low", choices=["low", "medium", "high"],
                    help="render size: low=480p short side (fast on CPU, default), medium=720p, high=1080p")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--seconds", type=float, default=10.0, help="target seconds per scene")
    ap.add_argument("--style", default="", help='e.g. "white background, pastel colors"')
    ap.add_argument("--retries", type=int, default=2, help="Gemini repair attempts per scene")
    ap.add_argument("--latex", action="store_true", help="manim only: allow Tex/MathTex (needs LaTeX installed)")
    ap.add_argument("--out", default="output")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    prompt = read_prompt(args).strip()
    if not prompt:
        sys.exit("Empty prompt.")

    if args.engine == "remotion":
        from remotion_video import generate_video
    else:
        from scene_video import generate_video
    try:
        video = generate_video(prompt, aspect=args.aspect, quality=args.quality, fps=args.fps,
                               scene_seconds=args.seconds, style=args.style, retries=args.retries,
                               allow_latex=args.latex, out_dir=args.out)
    except SceneError as e:
        sys.exit(f"\nFAILED: {e}\n(the failed code is saved in the build/ folder)")
    print(f"\nVideo ready: {video.resolve()}")


if __name__ == "__main__":
    main()
