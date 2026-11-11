# GPU VRAM Basics

VRAM is the graphics card's own memory. A model must fit in VRAM to run fast; layers that spill into system RAM run on the CPU and slow generation sharply. Leave headroom for the context window (KV cache) and the desktop itself. On a 4 GB card, keep the language model on the GPU and run speech tools on the CPU. See [[How Local LLMs Work]] and [[Quantization Explained]].
