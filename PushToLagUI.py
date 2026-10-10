"""Entry point for the rebuilt UI layer (backend/ + ui/). The legacy
PushToLag.py is untouched and still runnable separately."""
from ui.app import main

if __name__ == "__main__":
    main()
