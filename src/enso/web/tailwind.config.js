module.exports = {
  content: {
    relative: true,
    files: ["./templates/**/*.html", "./app.py"],
  },
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        canvas: "rgb(var(--enso-canvas) / <alpha-value>)",
        surface: "rgb(var(--enso-surface) / <alpha-value>)",
        "surface-muted": "rgb(var(--enso-surface-muted) / <alpha-value>)",
        "surface-hover": "rgb(var(--enso-surface-hover) / <alpha-value>)",
        border: "rgb(var(--enso-border) / <alpha-value>)",
        "border-strong": "rgb(var(--enso-border-strong) / <alpha-value>)",
        ink: "rgb(var(--enso-ink) / <alpha-value>)",
        muted: "rgb(var(--enso-muted) / <alpha-value>)",
        faint: "rgb(var(--enso-faint) / <alpha-value>)",
        action: "rgb(var(--enso-action) / <alpha-value>)",
        "action-hover": "rgb(var(--enso-action-hover) / <alpha-value>)",
        "action-text": "rgb(var(--enso-action-text) / <alpha-value>)",
        focus: "rgb(var(--enso-focus) / <alpha-value>)",
        overlay: "rgb(var(--enso-overlay) / <alpha-value>)",
      },
    },
  },
  plugins: [],
};
