#!/usr/bin/env python3
"""Deterministically generate and archive a Daily AI Art Drop through ComfyUI."""

import argparse
import copy
import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_COMFY_URL = 'http://127.0.0.1:8188'
DEFAULT_WORKFLOW = Path('/home/cflux/.hermes/workflows/daily_art_drop_krea_turbo_api.json')
DEFAULT_DECK = Path('/home/cflux/.hermes/scripts/art_drop_deck.py')
DEFAULT_ARCHIVE_ROOT = Path('/mnt/LargeStorage/art_archive')


class RunnerError(RuntimeError):
    pass


def read_json(response):
    return json.loads(response.read().decode('utf-8'))


def build_workflow(workflow_path, prompt, prefix, width=None, height=None, steps=None):
    workflow = copy.deepcopy(json.loads(Path(workflow_path).read_text(encoding='utf-8')))
    try:
        workflow['5']['inputs']['text'] = prompt
        workflow['9']['inputs']['filename_prefix'] = prefix
        if width is not None:
            workflow['4']['inputs']['width'] = width
        if height is not None:
            workflow['4']['inputs']['height'] = height
        if steps is not None:
            workflow['7']['inputs']['steps'] = steps
    except (KeyError, TypeError) as exc:
        raise RunnerError(f'workflow fixture has unexpected structure: {exc}') from exc
    if '__ART_DROP_' in json.dumps(workflow):
        raise RunnerError('workflow placeholder was not fully replaced')
    return workflow


def submit_workflow(base_url, workflow, opener=urlopen):
    url = base_url.rstrip('/') + '/prompt'
    body = json.dumps({'prompt': workflow}).encode('utf-8')
    request = Request(url, data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with opener(request, timeout=60) as response:
            result = read_json(response)
    except Exception as exc:
        raise RunnerError(f'ComfyUI prompt submission failed: {exc}') from exc
    prompt_id = result.get('prompt_id')
    if not isinstance(prompt_id, str) or not prompt_id:
        raise RunnerError(f'ComfyUI submission returned no prompt_id: {result}')
    return prompt_id


def execution_error_text(status):
    parts = []
    for message in status.get('messages', []):
        if isinstance(message, list) and message and message[0] == 'execution_error':
            detail = message[1] if len(message) > 1 and isinstance(message[1], dict) else {}
            parts.append(detail.get('exception_message') or str(detail))
    return '; '.join(parts) or status.get('status_str', 'unknown ComfyUI execution error')


def wait_for_completion(base_url, prompt_id, timeout_seconds=900, poll_seconds=3, opener=urlopen):
    deadline = time.monotonic() + timeout_seconds
    url = base_url.rstrip('/') + '/history/' + prompt_id
    while time.monotonic() <= deadline:
        try:
            with opener(Request(url, method='GET'), timeout=60) as response:
                history = read_json(response)
        except Exception as exc:
            raise RunnerError(f'ComfyUI history lookup failed for {prompt_id}: {exc}') from exc
        record = history.get(prompt_id)
        if record:
            status = record.get('status', {})
            if status.get('status_str') == 'error':
                raise RunnerError(f'ComfyUI execution failed for {prompt_id}: {execution_error_text(status)}')
            if status.get('status_str') == 'success':
                for output in record.get('outputs', {}).values():
                    for image in output.get('images', []):
                        if str(image.get('filename', '')).lower().endswith('.png'):
                            return image
                raise RunnerError(f'ComfyUI succeeded for {prompt_id} but returned no PNG output')
        time.sleep(poll_seconds)
    raise RunnerError(f'ComfyUI timed out after {timeout_seconds}s waiting for {prompt_id}')


def download_output(base_url, image, destination, opener=urlopen):
    filename = image.get('filename')
    if not filename:
        raise RunnerError(f'ComfyUI output missing filename: {image}')
    query = urlencode({
        'filename': filename,
        'subfolder': image.get('subfolder', ''),
        'type': image.get('type', 'output'),
    })
    request = Request(base_url.rstrip('/') + '/view?' + query, method='GET')
    try:
        with opener(request, timeout=120) as response:
            content = response.read()
    except Exception as exc:
        raise RunnerError(f'failed to download ComfyUI output {filename}: {exc}') from exc
    if not content:
        raise RunnerError(f'downloaded ComfyUI output is empty: {filename}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    if destination.stat().st_size == 0:
        raise RunnerError(f'downloaded ComfyUI output is empty: {destination}')
    return destination


def validate_png(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RunnerError(f'PNG source is missing or empty: {path}')
    if path.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
        raise RunnerError(f'PNG signature validation failed: {path}')
    return path


def archive_png(source, archive_root, month=None):
    source = validate_png(source)
    month = month or dt.date.today().strftime('%Y-%m')
    destination = Path(archive_root) / month / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RunnerError(f'archive copy is missing or empty: {destination}')
    return destination


def deck_prompt(deck_path):
    try:
        result = subprocess.run([sys.executable, str(deck_path)], check=True, text=True, capture_output=True, timeout=60)
    except Exception as exc:
        raise RunnerError(f'art prompt deck failed: {exc}') from exc
    prompt = result.stdout.strip()
    if not prompt:
        raise RunnerError('art prompt deck returned an empty prompt')
    return prompt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comfy-url', default=DEFAULT_COMFY_URL)
    parser.add_argument('--workflow', type=Path, default=DEFAULT_WORKFLOW)
    parser.add_argument('--deck', type=Path, default=DEFAULT_DECK)
    parser.add_argument('--archive-root', type=Path, default=DEFAULT_ARCHIVE_ROOT)
    parser.add_argument('--prompt', help='Override generated deck prompt (for controlled smoke tests).')
    parser.add_argument('--prefix', help='Unique ComfyUI SaveImage prefix.')
    parser.add_argument('--width', type=int)
    parser.add_argument('--height', type=int)
    parser.add_argument('--steps', type=int)
    parser.add_argument('--timeout-seconds', type=int, default=900)
    parser.add_argument('--poll-seconds', type=float, default=3)
    args = parser.parse_args(argv)

    now = dt.datetime.now()
    prefix = args.prefix or now.strftime('art_drop_%Y%m%d_%H%M%S')
    prompt = args.prompt or deck_prompt(args.deck)
    workflow = build_workflow(args.workflow, prompt, prefix, args.width, args.height, args.steps)
    prompt_id = submit_workflow(args.comfy_url, workflow)
    image = wait_for_completion(args.comfy_url, prompt_id, args.timeout_seconds, args.poll_seconds)
    generated = Path('/tmp') / f'{prefix}_{Path(image["filename"]).name}'
    downloaded = download_output(args.comfy_url, image, generated)
    archived = archive_png(downloaded, args.archive_root, month=now.strftime('%Y-%m'))
    print(json.dumps({
        'prompt': prompt,
        'prompt_id': prompt_id,
        'generated_png': str(downloaded),
        'archived_png': str(archived),
    }, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except RunnerError as exc:
        print(f'Daily AI Art Drop runner failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
