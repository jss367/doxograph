"""Run Loupe with a narrow compatibility shim for links from another local app.

Loupe rejects same-site navigation between different localhost ports. Only its
static HTML shell needs to accept a top-level browser navigation; API requests
and embedded documents retain Loupe's original request checks.
"""
import importlib.util
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit


def navigation_handler(base: type[Any], root: Path, survey: bool) -> type[Any]:
    # `base` is the handler class Loupe's untyped server builds at runtime.
    class Handler(base):  # type: ignore[misc]
        def do_GET(self) -> Any:
            if (urlsplit(self.path).path in {'/', '/index.html'}
                    and self.headers.get('Sec-Fetch-Mode') == 'navigate'
                    and self.headers.get('Sec-Fetch-Dest') == 'document'
                    and self.headers.get('Host') in {
                        f'127.0.0.1:{self.server.server_port}',
                        f'localhost:{self.server.server_port}',
                    }):
                filename = 'survey.html' if survey else 'index.html'
                return self.send((root / filename).read_bytes(), mime='text/html; charset=utf-8')
            return super().do_GET()
    return Handler


def main() -> None:
    server = Path(sys.argv[1])
    sys.argv = sys.argv[1:]
    sys.path.insert(0, str(server.parent))
    spec = importlib.util.spec_from_file_location('_doxograph_loupe', server)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load the Loupe server')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    make_handler = module.make_handler

    def linked_handler(*args: Any, **kwargs: Any) -> type[Any]:
        return navigation_handler(make_handler(*args, **kwargs), server.parent, '--survey' in sys.argv)

    module.make_handler = linked_handler  # type: ignore[attr-defined]  # Loupe's module, loaded from a path
    module.main()


if __name__ == '__main__':
    main()
