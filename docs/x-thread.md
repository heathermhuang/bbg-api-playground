# X Thread: Bloomberg Terminal API Playground Launch

Copy-paste each tweet below. Attach the corresponding screenshot image.

---

## Tweet 1/7 (Hook -- attach: hero-playground.png)

I open-sourced my Bloomberg Terminal.

Not the terminal itself -- a web playground that turns blpapi into a REST API you can query from any browser.

BDP, BDH, BDS, BQL, intraday data, yield curves -- all via GET requests.

github.com/heathermhuang/bbg-api-playground

---

## Tweet 2/7 (Table -- attach: table-view.png)

Every response auto-renders as a sortable table.

Mag 7 snapshot: price, daily change, P/E, market cap, analyst targets -- all from one API call.

Green/red color coding. Click headers to sort. No Excel needed to scan the data.

---

## Tweet 3/7 (AI Chat -- attach: ai-chat.png)

The best part: an AI assistant that speaks Bloomberg.

"How many cars did Tesla deliver last quarter?"

It knows that's a BQL field (NUMBER_OF_VEHICLES_SOLD), builds the API call, generates the Excel formula, and lets you run it with one click.

Powered by Claude.

---

## Tweet 4/7 (Formula Builder -- attach: formula-builder.png)

Built an Excel Formula Builder with 108 Bloomberg fields across 9 categories.

Pick security + field = instant =BDP(), =BDH(), =BDS(), or =BQL() formula.

It even tells you which fields work via API vs. which need the Bloomberg Excel Add-in.

---

## Tweet 5/7 (Excel Bridge -- attach: excel-bridge.png)

Getting Bloomberg data into Excel without the Add-in:

- Power Query M code (auto-refreshes)
- VBA macro (MSXML2.XMLHTTP60)
- Tab-separated copy (Ctrl+V into cells)
- CSV download

All auto-generated from whatever query you just ran.

---

## Tweet 6/7 (Mobile -- attach: mobile-responsive.png)

It's fully responsive too.

Sidebar becomes a drawer. Tabs scroll horizontally. Chat goes full-screen.

Same Bloomberg API playground, from your phone.

---

## Tweet 7/7 (CTA -- no image)

It's MIT licensed. Self-host it on your Bloomberg Terminal machine.

What you need:
- Bloomberg Terminal with blpapi
- Python 3.9+
- 3 commands to start

git clone, pip install, python proxy-playground.py

github.com/heathermhuang/bbg-api-playground

Star it if this is useful.
