# How Local LLMs Work

A local large language model is a file of weights run by an inference engine such as Ollama on your own machine. It predicts the next token repeatedly. Speed depends on memory bandwidth and whether the whole model fits in GPU memory ([[GPU VRAM Basics]]). Smaller or quantized models trade some quality for speed ([[Quantization Explained]]). Retrieval lets a small model answer from your own notes without retraining; see [[Vector Embeddings]].
