# Quantization Explained

Quantization stores model weights with fewer bits, for example 4-bit instead of 16-bit, cutting memory roughly 4x with a small quality loss. Q4_K_M is a popular balance. A 4 billion parameter model at Q4 needs about 2.5 GB, which fits a 4 GB graphics card ([[GPU VRAM Basics]]). The same idea shrinks speech models for [[Speech Recognition with Whisper]].
