---
name: card-designs
description: Design a card for the Hermit app - a saved layout you fill with data, with native blocks, inputs, buttons that call connectors, and cards that update in place. Read this before making or changing a card design.
---

# Designing cards for Hermit

A card design ("kind") is a saved layout for one sort of card: a pipeline, a morning brief, a deploy. You
design it once with `hermit_card_kind`; after that you send only data with `hermes_card(kind, data)`, and
every card of that sort looks the same. The app draws it natively, in light and dark, at any text size.

Make one when the user asks ("make me a card for my pipeline"), or when you notice you're laying out the same
sort of card again and again.

## Pick the shape

| The user wants | Build |
| --- | --- |
| Something to read at a glance | Blocks only: metrics, a list or table, a sparkline |
| To act on it | Add buttons: a `message` back to you, a `url`, or a `call` to a connector tool |
| To enter or choose something first | Add `input` blocks, and read them in a button as `{{input.<id>}}` |
| It to change while they watch | Give each card an `id`, and send it again with the same `id` |
| A mini app with its own screen | That's a mini app, not a card: see the mini-apps skill |

## Making one

Call `hermit_card_kind` with:

- `name`: lowercase and dashes, such as `pipeline-dashboard`.
- `description`: one sentence saying what it's for. You'll see it in every later message, so make it
  specific enough to know when to use it.
- `template`: the card, with `{{bindings}}` wherever data goes.
- `sample`: realistic data, shaped exactly as you'll send it later. The answer is a preview card the user
  sees, so make the sample look like the real thing.

To change a design, read it first with `hermit_card_kind(name, read: true)`, edit what came back, and save it
again under the same name. Never redesign from memory: you'd lose what the user already approved.

## Bindings

- `{{name}}` or `{{deal.amount}}` inside any string reads from `data`.
- A string that is only a binding, such as `"{{history}}"`, keeps a list or number as it is. Use that for a
  sparkline's `values`, a choice input's `options`, or a call's whole `with`.
- In a list's `items` or a table's `rows`, `{"each": "deals", "item": {...}}` repeats `item` for every entry of
  `data.deals`, with that entry's keys readable directly as `{{title}}`.
- `{{input.<id>}}` is left alone until the user taps a button, and then becomes what they entered.

## Blocks

Up to 24 per card. Use the fewest that tell the story, usually two to four.

| Block | For | Keys |
| --- | --- | --- |
| `metrics` | Two to four headline numbers | `items: [{label, value, delta, trend: up/down/flat, tint}]` |
| `sparkline` | A trend | `label`, `values` (numbers), `tint` |
| `progress` | How far along | `label`, `value` from 0 to 1, `detail`, `tint` |
| `list` | Items with a status | `items: [{title, detail, trailing, symbol, tint, done}]` |
| `table` | Rows and columns | `columns`, `rows` (at most 6 columns, 12 rows) |
| `chips` | Tags and flags | `items: [{text, tint}]` |
| `fields` | Label and value pairs | `items: [{label, value}]` |
| `text` | A sentence or a heading | `text`, `style`: body, caption or headline |
| `image` | A picture | `url` (https), `alt` |
| `input` | Something the user fills in | `id`, `kind`: text, toggle, choice, date or number, `label`, `options`, `value` |
| `divider` | A break | none |

Tints are green, red, orange, yellow, blue, purple and gray. A metric's arrow is green going up and red
going down, unless you set its `tint`: give spend, burn, errors and overdue counts a red or green tint
that says whether the change is good. `symbol` is an SF Symbol name, such as `calendar` or
`exclamationmark.circle`.

## Buttons

At most 8, with labels of one or two words. The last `primary` one is the filled button.

- `message`: sent to you as the user's reply, for anything that needs your judgment.
- `url`: an https link to open.
- `call`: one of your connector tools, by its exact name (`mcp__notion__...`), with `with` as its arguments,
  for a direct action that needs no thinking: mark done, change a stage, log an entry. It runs without a
  turn of yours, after the user confirms what exactly will run. Only connector tools can be called. Set
  `done` to the short status the card shows afterwards, and `show: true` when it's a read whose answer the
  user wants to see. Set `confirm: false` only for harmless reads. The user can turn these buttons off in
  Settings → Integrations → Card actions; if a tap tells you that, say so plainly.

## Keeping a card fresh

A card shows the data it was sent with. To keep one current:

- Give it an `id` (for example `pipeline` or `deploy-241`) and a **Refresh** button whose `message` asks you
  to send it again. When asked, fetch the data again and send the card with the same `id`: it updates in
  place instead of adding a new card.
- For a card the user wants on a schedule, such as a morning brief, make a routine that sends it.
- For something that changes every few seconds, use the status bar or a mini app instead.

## Rules

- Never invent data. A design is a layout; its sample is the only made-up data, and only in the preview.
- Keep card text short. Say anything longer, and anything about how you got the data, in your reply.
- The user may turn a design off in Settings → Card designs. If `hermes_card` says it's off, send a plain
  card and don't remake the design unless asked.
- After saving a design, tell the user in a sentence what it's for and how to ask for it.
