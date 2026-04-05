# Disclaimer, Terms of Use & Privacy Policy

## Disclaimer

This software ("Bloomberg API Playground") is an independent, community-developed, open-source project. It is **NOT** affiliated with, endorsed by, or supported by Bloomberg L.P. or any of its subsidiaries.

"Bloomberg", "Bloomberg Terminal", "BLPAPI", "BQL", and related terms are trademarks of Bloomberg L.P. Their use in this project is solely for descriptive purposes to indicate compatibility.

This software is provided "AS IS", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and noninfringement. In no event shall the authors or copyright holders be liable for any claim, damages, or other liability arising from the use of this software.

## Terms of Use

### Bloomberg Terminal Compliance

Users of this software are solely responsible for ensuring their use complies with:

- **Bloomberg Terminal Subscription Agreement**: Your Bloomberg Terminal subscription governs what data you may access, redistribute, or display. This tool does not grant any additional data rights beyond your existing Bloomberg subscription.
- **Bloomberg Data License terms**: Bloomberg data retrieved through this tool remains subject to Bloomberg's data redistribution policies. Do not expose Bloomberg data to unauthorized users.
- **BLPAPI usage guidelines**: The Bloomberg API (blpapi) is licensed for use only by authorized Bloomberg Terminal subscribers. Ensure your Terminal subscription includes API access.

### Potential Risks

- **Data redistribution**: Exposing this API server to a network may constitute data redistribution under your Bloomberg agreement. Run only on localhost or within your authorized network.
- **Session limits**: Bloomberg Terminals have connection limits. Excessive API usage may disrupt your Terminal session.
- **API key exposure**: AI provider API keys entered in settings are stored server-side in config.json. Protect this file appropriately.
- **Network exposure**: If you expose this server beyond localhost (e.g., via tunnel or public IP), any network user can query your Bloomberg Terminal data through the API.

### Your Responsibilities

1. You must have an active Bloomberg Terminal subscription with API access enabled
2. You must comply with all Bloomberg data policies regarding storage, display, and redistribution
3. You are responsible for securing access to this tool and any data it returns
4. You are responsible for any costs incurred from AI provider API usage (Anthropic, OpenAI, Google)
5. You must not use this tool to circumvent any Bloomberg licensing restrictions

## Privacy Policy

### Data Collection

This software does **NOT** collect, transmit, or store any user data externally. Specifically:

- **No analytics or tracking**: No telemetry, usage analytics, or tracking of any kind
- **No external data transmission**: The software only communicates with your Bloomberg Terminal (localhost), your configured OpenBB instance, and your chosen AI provider
- **No account creation**: No user accounts, registrations, or sign-ups

### Data Storage

- **API keys**: AI provider API keys are stored locally in `config.json` on the server filesystem. They are never transmitted except to the respective AI provider API.
- **Settings**: UI preferences (endpoint URLs, display name, provider selection) are stored in your browser's localStorage. They never leave your browser.
- **Chat history**: Conversation history exists only in browser memory during your session. It is not persisted to disk.
- **Bloomberg data**: Data retrieved from Bloomberg is returned directly to your browser and is not cached, stored, or logged by this software.

### Third-Party Services

When using the AI assistant, your chat messages (but not Bloomberg data) are sent to the selected AI provider:
- **Anthropic**: Subject to [Anthropic's Privacy Policy](https://www.anthropic.com/privacy)
- **OpenAI**: Subject to [OpenAI's Privacy Policy](https://openai.com/privacy)
- **Google**: Subject to [Google's Privacy Policy](https://policies.google.com/privacy)

### OpenBB Data

OpenBB endpoints may use third-party data providers (yfinance, FMP, FRED, etc.). Each provider has its own terms of service and data usage policies. You are responsible for complying with those terms.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Contact

For questions about this software, open an issue on [GitHub](https://github.com/heathermhuang/bbg-api-playground).
