# Test report

Run on 2026-11-11 on the target machine. Every result below was executed; nothing is inferred.

## Summary

| Suite | Result |
|---|---|
| `pytest` (59 tests) | **58 passed, 1 expected failure**, 91 s |
| `preflight.py` (live system, 26 checks) | **24 pass, 0 fail, 2 warn**, exit 0 |
| `drill.py` (network audit) | **Passed**: no host outside localhost |
| Browser check (Playwright) | Passed: page, four tabs, chat, memory, model switch |
| Offline test with the network physically off | **Not run** (see OFFLINE_MODE.md) |

The expected failure and one warning are the same finding: Ollama listens on all network interfaces on this machine.
The other warning is low free RAM (1.4 GB) at the time of the run.

## Interface redesign (command centre) — re-run after the change

| Suite | Result |
|---|---|
| `pytest` (62 tests, 3 new) | **61 passed, 1 expected failure**, 102 s |
| `preflight.py` | **25 pass, 0 fail, 1 warn**, exit 0 |
| `drill.py` | Passed; 0 external references in `viewer/` |

There is no frontend build step (plain ES modules), so there was no build to run.

Checked in a real browser (Playwright) at 1920×1080, 1600×900, 1366×768 and 700×820:

| Area | Result |
|---|---|
| Layout | No page scroll at the three desktop sizes; on the small size the rail collapses, the HUD hides, the command bar spans the width |
| Left rail | Real counts (30 notes, 80 connections, 4 categories), hubs, category hide/show, search with Enter, inspector with related notes |
| Galaxy | Select, dim unrelated, labels, fly to the source of an answer, fit, reset; 144 fps with a 7 ms worst frame, including while the model was generating |
| Chat | Streaming, markdown, typing indicator, copy, clear (server history emptied), follow-ups |
| Stop generation | UI stopped in 106 ms; the next turn answered in 2.7 s, so the server released the abandoned turn |
| Commands | "show my notes" opened the explorer; "find information about spaced repetition" listed and highlighted 3 notes |
| Notes | Create, edit (markdown and wikilinks rendered), delete with confirmation (moved to trash), note ↔ star selection |
| System | Live CPU, RAM, GPU, VRAM, Ollama, audio devices from the backend; settings load and save |
| Modes | HOLO and FOCUS toggle; REFLEX switched the model to `qwen3:4b` and back; EYES is disabled and labelled not implemented |
| Microphone | Real device: on → listening, push-to-talk → recording with level events → back to listening, off → released |
| Network | Every resource loaded from `127.0.0.1:4700` |

Speaking state: one line was spoken through `/api/speak`; the reactor switched to its speaking animation and the stop button
appeared. Not verified: stopping speech from the new stop button mid-sentence (the line had finished before the click); nobody
spoke into the microphone; double-click explore and drag/zoom were not driven by the test (they use the library's own handlers).
One idle frame-rate reading right after a page load was 1 fps and could not be reproduced (145 fps on re-measurement).

Found and fixed during this pass: the model invented the weather for "Hello Jarvis" (persona now forbids made-up facts about the
user's surroundings and is given the local time); the camera flew too close to a selected note; the stop button showed while idle.

## Automated tests

| File | Tests | Covers |
|---|---|---|
| `tests/test_core.py` | 13 | Configuration validation, notes indexing, graph generation, memory create/update/safe delete, external file changes, retrieval ranking, history persistence and window, persona budget |
| `tests/test_brain.py` | 5 | Streaming, reasoning never leaks, friendly errors when Ollama is down or a model is missing, model switching unloads the old model and never pulls (mocked Ollama) |
| `tests/test_api.py` | 17 | Backend startup, chat, streaming, history, model listing/switching, memory, notes, graph, config, input validation, central error handling, security guards, private files, offline enforcement, no CDN |
| `tests/test_speech.py` | 13 | Speech text cleaning, sentence chunking, WAV parsing, stop-speaking, microphone release and failure handling (mocked device); real Piper, real Whisper round trip, real wake word, real playback, real microphone |
| `tests/test_live.py` | 7 | Real Ollama: connectivity, streaming, identity, multi-turn context, model switching, thinking model, full hands-free chain |
| `tests/test_preflight.py` | 4 | Preflight helpers, config, sample notes |

Tests that use real hardware or models skip with a reason if the resource is missing. None skipped in this run.

## What the hardware-dependent tests did, and did not, prove

| Area | Proven | Not proven |
|---|---|---|
| Voice output | Piper synthesised and the default output device played audio without error; stop took 0.19 s | Nobody listened to it. Audio quality is unjudged |
| Voice input | Whisper returned Piper's sentence word for word (7–8 of 8 words across runs) | Recognition of your voice, accent and room |
| Microphone | The real default device opened, delivered frames at the expected rate, and was released | Speech captured through it |
| Wake word | "Hey Jarvis" scored 1.00; "Hey Travis" and unrelated speech scored 0.00 (synthetic voice) | Your voice; false triggers over hours of use. A bare "Jarvis" scored 0.44, below the threshold |
| Hands-free chain | Synthetic "Hey Jarvis. What is quantization?" fed in as microphone audio → wake → record → Whisper → notes → Ollama → Piper | The same with a person speaking |

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Backend starts | Pass |
| 2 | Frontend starts | Pass (served by the same process) |
| 3 | Local model responds correctly | Pass |
| 4 | Streaming works | Pass |
| 5 | Voice output works | Pass, with the caveat above |
| 6 | Voice input works | Pass on synthetic speech; your voice untested |
| 7 | Wake word works | Pass on synthetic speech ("Hey Jarvis"); your voice untested. Push-to-talk is the fallback |
| 8 | Memory persists after restart | Pass (note found after stop/start) |
| 9 | Notes retrieval works | Pass |
| 10 | 3D galaxy works | Pass (render, search, click, live new node, fly to source) |
| 11 | Model switching works | Pass (API, UI, spoken command; old model unloaded) |
| 12 | Works with internet disconnected | **Open**: drill passes, physical test not run |
| 13 | All automated tests pass | 58 of 59; the one expected failure is an environment finding |
| 14 | No unexpected cloud calls | Pass |
| 15 | Documentation complete | Pass |

## Defects found by testing and fixed

- The first preflight passed a reply that was pure model reasoning; the check now detects it.
- The model's default 262k context pushed it to 7.4 GB and an 83 s first reply; context is fixed at 4096.
- Camera flew to the wrong note after a new memory (numeric ids shift); the UI now uses file paths.
- Greetings such as "good morning" retrieved the "Morning Routine" note; greetings are now treated as small talk.
- The stop script left the real server running (the venv launcher spawns a child); it now stops the process tree.
- A sliding history window forced the model to re-read the whole prompt every turn; see HARDWARE_BENCHMARK.md.

## Not covered

Phone-sized layout, note collections in the thousands, hours-long runs (memory growth), and echo when using loudspeakers.
