# Classic Chess Python API client

Read curated chess archives, player biographies, notable games, annotated books,
MasterDB games and opening statistics. Public reads need no API key. The client
uses only Python's standard library and includes a command-line interface.

## Install

Requires Python 3.10 or newer. From this public repository's root:

```sh
python3 -m pip install .
classicchess-api public players --names
```

The package is not published to PyPI yet. You can also download and run the
[standalone script](https://raw.githubusercontent.com/Andromeda1957/classicchess-api-python/main/pull_api.py):

```sh
curl -fsS https://raw.githubusercontent.com/Andromeda1957/classicchess-api-python/main/pull_api.py -o pull_api.py
python3 pull_api.py public players --names
python3 pull_api.py public notable mikhail-tal
```

## Python

```python
from andromeda_api import AndromedaClient

api = AndromedaClient()
games = api.public_games(archive_player="mikhail-tal", all_pages=True, limit=150)
for game in games["results"]:
    print(game["token"], game["white"], game["black"])
```

Game searches are paginated; `all_pages=True` follows the complete sequence and
`limit` stops early. Each PGN export is capped at 300 games. Use exact slugs and
resource URLs returned by the API. Account operations require a scoped personal
API token; keep it out of source code and send it only to the trusted HTTPS origin.

See the [API reference](https://classicchess.com/api/) for endpoints, errors,
limits and account authentication. The other maintained SDKs are
[TypeScript](https://github.com/Andromeda1957/classicchess-api-typescript) and
[Kotlin](https://github.com/Andromeda1957/classicchess-api-kotlin).

## Tests

```sh
python3 -m unittest discover -s andromeda_api
```

MIT licensed; see [LICENSE](LICENSE). This repository receives tested exports
from the private development monorepo. See [CONTRIBUTING.md](CONTRIBUTING.md)
for how changes are incorporated.
