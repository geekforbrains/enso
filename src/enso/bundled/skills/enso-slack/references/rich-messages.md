# Rich messages

`enso slack send -c CHANNEL --rich message.json` accepts a bare or fenced `enso-message`
envelope. Slack chat turns also supply this contract for replies:

```json
{"version":1,"fallback_text":"Summary","blocks":[{"type":"markdown","text":"Details"}]}
```

Supported blocks:

- Markdown: `{"type":"markdown","text":"…"}`.
- Table: `{"type":"table","rows":[["Region","Units"],["North","1,240"]],"columns":[{},{"align":"right"}]}`.
  Rows have equal length; the first is the header. Format numbers, money, and percentages
  as display text. Numeric columns default to right alignment unless overridden.
  Limits: 100 rows, 20 columns, 10,000 characters per message.
- Bar chart: `{"type":"chart","kind":"bar","title":"Units","categories":["North"],"series":[{"name":"Units","data":[1240]}]}`.
- Pie chart: `{"type":"chart","kind":"pie","title":"Share","segments":[{"label":"North","value":1}]}`.

At most two charts per message; charts require titles. Supply useful `fallback_text`.
