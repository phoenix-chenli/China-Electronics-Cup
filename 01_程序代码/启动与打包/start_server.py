"""Native local service entry point. Paths never depend on the shell directory."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_bootstrap import activate
activate()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.main import app
import uvicorn
if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8765, log_level='warning')
