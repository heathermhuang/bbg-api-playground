# Changelog

## v1.4.0 (2026-04-05)
- OpenBB Python SDK examples with 69 endpoint snippets
- Context-aware tabs: "Python SDK" and "Python" tabs when on OpenBB
- Formula Builder and Excel Bridge hidden for OpenBB (Bloomberg-only)

## v1.3.0 (2026-04-05)
- Security hardening: per-IP rate limiting (60/min Bloomberg, 20/min chat)
- CF-Connecting-IP spoofing fix across all proxy servers
- Server-side message limits (30 messages, 10KB per message)
- Error message sanitization to prevent internal detail leakage
- API docs gated behind ENABLE_DOCS environment variable
- Request logging middleware with client IP, method, timing

## v1.2.0 (2026-04-04)
- Full QA and security audit with 10 findings fixed
- CORS restricted to localhost-only origins
- Google API key moved from URL parameter to header
- Streaming error surfacing for all AI providers
- Array sort immutability fix (prevents data corruption)
- Health polling pauses on background tabs
- Chat history bounded to 20 messages client-side
- Mobile responsive layout improvements

## v1.1.0 (2026-04-04)
- Multi-provider AI support: Anthropic Claude, OpenAI GPT, Google Gemini
- User-selectable provider, model, and API key in settings
- BQL endpoint with automatic BDP fallback for unlicensed terminals
- BQL regex fix for nested parentheses in fiscal period ranges
- Tab button styling fix for ARIA-compliant button elements

## v1.0.0 (2026-04-04)
- Initial open-source release
- REST API wrapper for Bloomberg Terminal (blpapi)
- BDP, BDH, BDS, BQL, intraday bars/ticks, field catalog, security lookup, yield curves
- Interactive web playground with 7 tabs (Response, Table, Chart, Bloomberg Code, Excel, Formula Builder, Excel Bridge)
- AI assistant for natural language API query building
- Excel Bridge: Power Query M code, VBA macro generator, TSV copy, CSV download
- Formula Builder: 108 Bloomberg fields across 9 categories
- OpenBB API integration with 69 example endpoints
- IP whitelist proxy with configurable upstream routing
- Mobile responsive with sidebar drawer
