"""
Voice-to-text for Meta video ads.

  video_sd_url (from the Ad Library node)
    -> download (curl_cffi, Chrome impersonation; CDN URLs are signed and
       expire after ~1-2 days, so transcribe soon after collecting)
    -> ffmpeg: strip video, 16 kHz mono wav
    -> faster-whisper (CTranslate2, CPU int8): transcript + language +
       per-segment timestamps

Outputs one record per ad in ``meta_ads_transcripts_cache.json``:
  {ad_archive_id: {language, language_prob, duration_sec, transcript,
                   first_3s_hook, segments:[{start,end,text}], model, error}}

Whisper handles Hindi, Tamil, Telugu, Malayalam, Kannada, Bengali etc.
Code-mixed Hinglish usually comes out romanised or in Devanagari depending
on which the speaker leans on; we keep it as-is and don't translate.

Model choice: ``small`` is the sweet spot on CPU for Indian-language ad
audio (15-40s clips → ~10-20s each). Use ``medium`` for a quality pass on
a shortlist. ``base``/``tiny`` are noticeably worse on Tamil/Malayalam.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as creq

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "meta_ads_transcripts_cache.json")
MEDIA_DIR = os.path.join(ROOT, "media_cache", "ad_audio")

_model = None


def _get_model(size="small"):
    global _model
    if _model is None:
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel
        # greedy (beam 1) + all cores: 4x faster than beam 5 on CPU, same text on ad-length clips
        _model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    return _model


def load_cache() -> dict:
    if os.path.exists(CACHE):
        return json.load(open(CACHE, encoding="utf-8"))
    return {}


def save_cache(c: dict) -> None:
    tmp = CACHE + ".tmp"
    json.dump(c, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE)


def download_audio(ad_id: str, video_url: str) -> str:
    """Download the video and return the path of a 16 kHz mono wav."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    wav = os.path.join(MEDIA_DIR, f"{ad_id}.wav")
    if os.path.exists(wav) and os.path.getsize(wav) > 1000:
        return wav
    r = creq.get(video_url, impersonate="chrome120", timeout=90)
    if r.status_code != 200 or len(r.content) < 5000:
        raise RuntimeError(f"download failed status={r.status_code} bytes={len(r.content)}")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False, dir=MEDIA_DIR) as f:
        f.write(r.content)
        mp4 = f.name
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp4, "-vn",
                        "-ac", "1", "-ar", "16000", "-f", "wav", wav], check=True, timeout=120)
    finally:
        os.remove(mp4)
    return wav


def transcribe_wav(wav: str, model_size="small") -> dict:
    model = _get_model(model_size)
    segments, info = model.transcribe(wav, beam_size=1, vad_filter=True,
                                      vad_parameters={"min_silence_duration_ms": 400})
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
    text = " ".join(s["text"] for s in segs).strip()
    hook = " ".join(s["text"] for s in segs if s["start"] < 3.5).strip()
    return {
        "language": info.language, "language_prob": round(info.language_probability, 3),
        "duration_sec": round(info.duration, 1), "transcript": text,
        "first_3s_hook": hook, "segments": segs, "model": f"faster-whisper-{model_size}",
        "has_speech": bool(text), "word_count": len(text.split()),
    }


def transcribe_ads(ads: list[dict], model_size="small", workers=3, log=print) -> dict:
    """ads: records with ad_archive_id + video_url. Downloads in parallel,
    transcribes sequentially (whisper is already multi-threaded)."""
    cache = load_cache()
    todo = [a for a in ads if a.get("video_url") and a["ad_archive_id"] not in cache]
    log(f"transcribe: {len(todo)} to do, {len(cache)} cached")

    def dl(a):
        try:
            return a["ad_archive_id"], download_audio(a["ad_archive_id"], a["video_url"]), None
        except Exception as e:
            return a["ad_archive_id"], None, str(e)[:200]

    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        for i, (aid, wav, err) in enumerate(ex.map(dl, todo), 1):
            if err:
                cache[aid] = {"error": err, "has_speech": False}
            else:
                try:
                    cache[aid] = transcribe_wav(wav, model_size)
                except Exception as e:
                    cache[aid] = {"error": str(e)[:200], "has_speech": False}
            if i % 10 == 0 or i == len(todo):
                save_cache(cache)
                ok = sum(1 for a in todo[:i] if cache.get(a["ad_archive_id"], {}).get("has_speech"))
                log(f"  {i}/{len(todo)}  speech={ok}  {time.time()-t0:.0f}s")
    save_cache(cache)
    return cache


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("master", nargs="?", default=os.path.join(ROOT, "all_categories_meta_ads_master_v2.json"))
    ap.add_argument("--model", default="small")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--brand")
    a = ap.parse_args()
    ads = json.load(open(a.master, encoding="utf-8"))
    ads = [x for x in ads if x.get("has_video") == "Yes"]
    if a.brand:
        ads = [x for x in ads if a.brand.lower() in x["brand"].lower()]
    if a.limit:
        ads = ads[:a.limit]
    transcribe_ads(ads, a.model)
