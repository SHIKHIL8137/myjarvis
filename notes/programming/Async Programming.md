# Async Programming

Async code lets one thread wait on many slow operations (network, disk, microphone) without blocking. In Python use `async def` and `await`; CPU-heavy work such as speech recognition should go to a thread or process instead. A voice assistant must not freeze its UI while the model thinks. Web servers like those in [[REST API Design]] rely on this. CPU-bound models are covered in [[How Local LLMs Work]].
