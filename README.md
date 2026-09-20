# proxy_auto_config

Parse a PAC file and evaluate its JavaScript logic to determine the proxy for a given URL, with zero third-party dependencies.

```python
from proxy_auto_config import PACFile

pac = PACFile('function FindProxyForURL(url, host) { return "DIRECT"; }')
result = pac.find_proxy_for_url("http://example.com/")
print(result.proxies)  # ('DIRECT',)
```

## Why this exists

Proxy Auto Config files are small JavaScript programs used by browsers and other HTTP clients to decide which proxy server (if any) to use for a request. Evaluating them normally requires a full JavaScript engine. This library implements a minimal, sandboxed interpreter that supports the JavaScript subset used in typical PAC files, plus the standard PAC helper functions (`isPlainHostName`, `dnsDomainIs`, `shExpMatch`, etc.).

The main trade-off is that the interpreter is not a complete ECMAScript implementation. It supports variable declarations, function declarations, `if`/`else`, `return`, boolean and arithmetic expressions, and the PAC helpers. Uncommon constructs like loops, arrays, and object literals are intentionally unsupported. This keeps the implementation small and auditable while covering the vast majority of real PAC files.

## Awkward edge

PAC files often call helper functions that rely on network information, such as `isResolvable` or `myIpAddress`. Because this library runs in a sandbox with no network access, those functions return conservative defaults: `isResolvable` always returns `false`, `dnsResolve` returns an empty string, and `myIpAddress` returns `127.0.0.1`. PAC files that depend on these values may behave differently than in a browser.

## API

### `PACFile(source: str)`

Create a PAC file evaluator from JavaScript source.

### `PACFile.find_proxy_for_url(url: str) -> ProxyConfig`

Evaluate the PAC file for the given URL and return a `ProxyConfig`. The URL must be a full URL such as `http://example.com/`; the host is extracted from it. If the PAC file does not define `FindProxyForURL`, an `EvaluationError` is raised.

### `ProxyConfig`

A frozen dataclass with two attributes:

- `proxies`: a tuple of proxy strings, e.g. `("DIRECT",)` or `("PROXY proxy1:8080", "DIRECT")`
- `raw`: the exact string returned by `FindProxyForURL`
