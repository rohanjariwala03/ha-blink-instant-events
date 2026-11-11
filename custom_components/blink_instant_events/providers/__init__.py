"""Camera providers: where cameras, pictures and motion come from.

Each provider is a package with `async_setup_entry`, `async_unload_entry` and `PLATFORMS`, plus one module
per platform it offers. A config entry names its provider in `entry.data["provider"]`; entries made before
there were providers are Blink accounts.
"""
