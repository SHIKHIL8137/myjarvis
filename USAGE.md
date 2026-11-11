# Usage

```powershell
.\start-jarvis.ps1      # start; opens http://127.0.0.1:4700/
.\stop-jarvis.ps1       # stop
```

## The screen

| Area | What it is |
|---|---|
| Centre | The 3D knowledge galaxy: every note is a star, coloured by folder, linked by real `[[wikilinks]]` and title mentions |
| Left rail | Search, inspector for the selected note, top hubs, category filter, statistics, force sliders, display options. The « button collapses it |
| Top bar | **Fit**, **Reset** (camera), and the **Notes**, **Chat** and **System** drawers |
| Right | The Jarvis reactor with online status, current model, voice, microphone state, and four mode buttons |
| Bottom | The command bar. It stays available whatever else is open |

## Talking

| You want to | Do this |
|---|---|
| Type a command | Use the command bar (press `/` to jump to it) |
| Go hands-free | Click the microphone button, then say "Hey Jarvis", pause briefly, and ask |
| Ask without the wake word | Click the ● button, then speak |
| Silence spoken replies | Click the speaker button (🔊 / 🔇) |
| Stop a reply | Click ■ (appears while he is generating or speaking), or say "Hey Jarvis" |
| Read the whole conversation | Click the reply bubble or **Chat**. Each reply has a **copy** link; **Clear** wipes the history |

The reactor shows the state: slow spin = standby, breathing core = waiting for the wake word, red with live bars = recording you
(the bars follow the real microphone level), fast amber spin = transcribing or thinking, green bars = speaking, grey = backend down.
The bars while speaking are an animation, not a measurement of the audio.

## Commands he acts on directly

These are handled by code, not by the model, and the reply confirms what actually happened. They work typed or spoken.

| Say or type | Result |
|---|---|
| "Remember that …", "Note to self …", "Don't let me forget …", "Log this …" | Saves a markdown note in `notes/captures/`; a new star is born and the camera flies to it |
| "Show my notes" | Opens the knowledge explorer |
| "Find information about …", "Search my knowledge for …" | Lists matching notes and lights them up in the galaxy |
| "Switch to the thinking model" / "… the fast model" | Changes the local model |
| "Which model are you using?", "How many notes do you have?" | Answers from the system, not from the model |
| "Stop" | Stops speaking |

Anything else ("Explain …", questions, follow-ups) goes to the model, with your most relevant notes attached.

## The galaxy

| Action | Result |
|---|---|
| Drag / scroll / right-drag | Rotate / zoom / pan |
| Click a star | Focus it: it and its connections stay lit, the rest dim, and the note opens in the inspector |
| Double-click a star | Explore: lights everything within two steps |
| Click empty space | Clear the selection |
| Type in **Search the brain** | Highlights matches; Enter opens the best one (title matches first) |
| Click a category in the rail | Hide or show that category |
| **Repel** / **Link length** | Spread the graph out or pull it together |

Labels are shown for the main hubs and for whatever is highlighted.

## Mode buttons

| Button | What it does |
|---|---|
| **EYES** | Disabled. Vision (camera or screen) is not implemented |
| **HOLO** | Hologram view: scanlines, brighter connections, faster rotation |
| **FOCUS** | Hides the rail and HUD details so only the galaxy and command bar remain |
| **REFLEX** | On = the fast model. Off = the thinking model, which reasons first and is much slower |

## Notes (knowledge explorer)

Filter by text or category, read a note with its formatting and related notes, follow `[[links]]`, create, edit, or delete.
Delete asks for confirmation and only moves the file to `notes/.trash/`. Selecting a note selects its star.
Files you add or edit outside the app are picked up within a few seconds.

## System

Live monitor (CPU, RAM, GPU, VRAM, Ollama, audio devices, voice loop, network guard) read from the backend every 3 seconds while
the drawer is open. Settings: model, context window, voice, speed, speaker, microphone, wake word, accent colour, reduced motion.
Press **Save** to apply; toggle the microphone off and on after changing the input device.

To change the default model or the persona, edit `config.json` (`boot_brain`, `brains`) or `jarvis/persona.py` and restart.
