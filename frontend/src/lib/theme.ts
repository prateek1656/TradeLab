export type ThemePref = "dark" | "light" | "system";
const KEY = "tl-theme";

export function getThemePref(): ThemePref {
  try {
    return (localStorage.getItem(KEY) as ThemePref) || "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(pref: ThemePref) {
  const resolved = pref === "system" ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") : pref;
  document.documentElement.dataset.theme = resolved;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", resolved === "light" ? "#f4f5f7" : "#0c0e11");
  try {
    localStorage.setItem(KEY, pref);
  } catch {
    /* ignore */
  }
}
