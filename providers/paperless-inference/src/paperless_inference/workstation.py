"""Keep the former workstation imports and module command compatible."""

import json
import sys

from paperless_inference.processor import APIError as APIError
from paperless_inference.processor import Processor
from paperless_inference.processor import main as main
from paperless_inference.processor import map_files as map_files

Workstation = Processor

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__}), file=sys.stderr)
        sys.exit(1)
