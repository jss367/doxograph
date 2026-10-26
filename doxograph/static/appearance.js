'use strict';

// Appearance is deliberately a browser preference rather than corpus state:
// two people can view the same corpus without changing each other's display.
// Each color theme is a coordinated palette in light, dim, and dark variants,
// so switching it cannot accidentally leave text unreadable on its background.
// Dim is a softer dark mode: charcoal surfaces instead of near-black ones.
const THEME_STORAGE_KEY = 'doxograph-theme-v1';
const APPEARANCES = ['system', 'light', 'dim', 'dark'];
const DEFAULT_THEME = Object.freeze({ appearance: 'system', colors: 'slate' });
const THEME_PALETTES = {
  slate: {
    light: { ink: '#16181d', muted: '#5c6370', line: '#dfe2e8', bg: '#ffffff', panel: '#f6f7f9', accent: '#2f5d8a', warn: '#8a5a2f', ok: '#2f7a4f', sel: '#e8f0f8', 'accent-ink': '#ffffff' },
    dim: { ink: '#e3e5e9', muted: '#a2a8b3', line: '#454b56', bg: '#262931', panel: '#2e323b', accent: '#8db9e4', warn: '#dcab72', ok: '#8ccda5', sel: '#324152', 'accent-ink': '#121a24' },
    dark: { ink: '#e6e8ec', muted: '#99a0ad', line: '#343a44', bg: '#14161a', panel: '#1d2027', accent: '#7fb0e0', warn: '#d9a267', ok: '#7fc79b', sel: '#1f2a35', 'accent-ink': '#101820' },
  },
  forest: {
    light: { ink: '#17231b', muted: '#59695e', line: '#d7e2da', bg: '#fbfdfb', panel: '#f1f6f2', accent: '#2b6e48', warn: '#8a6028', ok: '#287447', sel: '#e2f1e7', 'accent-ink': '#ffffff' },
    dim: { ink: '#e2e9e4', muted: '#a1b0a5', line: '#43524a', bg: '#242c27', panel: '#2c352f', accent: '#82cc9f', warn: '#d9b074', ok: '#82cc9f', sel: '#2f4438', 'accent-ink': '#12241a' },
    dark: { ink: '#e4ece6', muted: '#98a99d', line: '#324239', bg: '#121914', panel: '#1a241d', accent: '#76c596', warn: '#d7aa68', ok: '#76c596', sel: '#1d3325', 'accent-ink': '#102018' },
  },
  plum: {
    light: { ink: '#241a27', muted: '#6c5e70', line: '#e2dbe5', bg: '#fefcff', panel: '#f7f2f8', accent: '#765183', warn: '#96603e', ok: '#477452', sel: '#f0e5f3', 'accent-ink': '#ffffff' },
    dim: { ink: '#ece6ee', muted: '#b3a6b7', line: '#55485a', bg: '#2b242e', panel: '#342c37', accent: '#c9a6d4', warn: '#dfa87d', ok: '#90c99b', sel: '#43354a', 'accent-ink': '#28182e' },
    dark: { ink: '#eee7f0', muted: '#ad9eb1', line: '#443848', bg: '#181319', panel: '#231c25', accent: '#c29bce', warn: '#dda071', ok: '#86c392', sel: '#34263a', 'accent-ink': '#24152a' },
  },
  sepia: {
    light: { ink: '#2d251d', muted: '#716556', line: '#ded3c2', bg: '#fcf8f1', panel: '#f4ecdf', accent: '#8a5138', warn: '#94621f', ok: '#52703c', sel: '#efe1d2', 'accent-ink': '#ffffff' },
    dim: { ink: '#ece4d7', muted: '#b6a893', line: '#5a4e41', bg: '#2e2822', panel: '#37302a', accent: '#dca383', warn: '#dfb676', ok: '#a2c388', sel: '#4a3a2f', 'accent-ink': '#2a1a11' },
    dark: { ink: '#eee5d7', muted: '#b1a28e', line: '#493e32', bg: '#1b1713', panel: '#28211b', accent: '#d99a78', warn: '#ddb06a', ok: '#99bd7d', sel: '#3b2b22', 'accent-ink': '#27170f' },
  },
  ocean: {
    light: { ink: '#132029', muted: '#556873', line: '#d3e0e6', bg: '#fafdfe', panel: '#eef5f8', accent: '#1f6f8b', warn: '#8d5f2a', ok: '#2f7a5f', sel: '#dfeff5', 'accent-ink': '#ffffff' },
    dim: { ink: '#e0e8ec', muted: '#9fb0b8', line: '#3e505a', bg: '#212b31', panel: '#293439', accent: '#6fc0dc', warn: '#d9ac70', ok: '#7fcbb0', sel: '#2c4250', 'accent-ink': '#0f2028' },
    dark: { ink: '#e2eaee', muted: '#94a7b0', line: '#2f414a', bg: '#0f181d', panel: '#172228', accent: '#63b6d3', warn: '#d6a565', ok: '#77c4a8', sel: '#193039', 'accent-ink': '#0b1a20' },
  },
  indigo: {
    light: { ink: '#181a2f', muted: '#5e627f', line: '#dddfeb', bg: '#fdfdff', panel: '#f3f4fa', accent: '#4a55a8', warn: '#8d5d2f', ok: '#3a7756', sel: '#e6e8f8', 'accent-ink': '#ffffff' },
    dim: { ink: '#e5e6ee', muted: '#a5a8bd', line: '#474b64', bg: '#262838', panel: '#2e3142', accent: '#9ea8ee', warn: '#dcab72', ok: '#8bcaa4', sel: '#353a5a', 'accent-ink': '#121530' },
    dark: { ink: '#e7e8f0', muted: '#9a9db4', line: '#35384f', bg: '#14151f', panel: '#1c1e2a', accent: '#939ee9', warn: '#d9a267', ok: '#7fc79b', sel: '#262a45', 'accent-ink': '#0f1128' },
  },
  rose: {
    light: { ink: '#2a1a1f', muted: '#726068', line: '#e8d9de', bg: '#fffbfc', panel: '#faf1f4', accent: '#a34a63', warn: '#95622a', ok: '#4b7a55', sel: '#f7e4ea', 'accent-ink': '#ffffff' },
    dim: { ink: '#efe6e9', muted: '#b5a3aa', line: '#584750', bg: '#2d2429', panel: '#372c32', accent: '#e397ad', warn: '#dfae76', ok: '#93c99a', sel: '#4a353f', 'accent-ink': '#2a141c' },
    dark: { ink: '#f0e6ea', muted: '#ad9aa2', line: '#47363d', bg: '#1a1316', panel: '#251c20', accent: '#dd8ba3', warn: '#dba46c', ok: '#8cc394', sel: '#3a2730', 'accent-ink': '#28121a' },
  },
  graphite: {
    light: { ink: '#1c1c1e', muted: '#66666b', line: '#e0e0e3', bg: '#ffffff', panel: '#f4f4f5', accent: '#9a5f10', warn: '#8a5a2f', ok: '#3d7a4a', sel: '#f5ebdc', 'accent-ink': '#ffffff' },
    dim: { ink: '#e6e6e8', muted: '#a6a6ab', line: '#4a4a4f', bg: '#28282b', panel: '#303034', accent: '#e0b060', warn: '#d9a267', ok: '#8fc79b', sel: '#3d3830', 'accent-ink': '#1e1608' },
    dark: { ink: '#e8e8ea', muted: '#9b9ba0', line: '#38383d', bg: '#161618', panel: '#1f1f22', accent: '#d8a852', warn: '#d9a267', ok: '#7fc79b', sel: '#332d22', 'accent-ink': '#1b1405' },
  },
  amber: {
    light: { ink: '#302414', muted: '#746044', line: '#e5d5b8', bg: '#fffaf0', panel: '#f8efd9', accent: '#945800', warn: '#94501c', ok: '#486a32', sel: '#f4e3bd', 'accent-ink': '#ffffff' },
    dim: { ink: '#f4e7cd', muted: '#c4af8c', line: '#62503a', bg: '#30291f', panel: '#3b3225', accent: '#f2bc60', warn: '#f0a477', ok: '#adcd85', sel: '#514027', 'accent-ink': '#30200a' },
    dark: { ink: '#f9e8c9', muted: '#bca581', line: '#4e3e28', bg: '#1d170f', panel: '#292014', accent: '#efb34e', warn: '#eba070', ok: '#a6c87b', sel: '#402f17', 'accent-ink': '#271a07' },
  },
  lavender: {
    light: { ink: '#29223b', muted: '#6c607e', line: '#dfd7eb', bg: '#fcfaff', panel: '#f1ecf9', accent: '#7150a2', warn: '#8c5a29', ok: '#356b50', sel: '#e9def8', 'accent-ink': '#ffffff' },
    dim: { ink: '#eee7f8', muted: '#b9accd', line: '#564a6b', bg: '#2c2637', panel: '#362e44', accent: '#c5a9f0', warn: '#dfb17b', ok: '#8fd0ae', sel: '#49395f', 'accent-ink': '#251536' },
    dark: { ink: '#f0e9fa', muted: '#b0a1c6', line: '#443751', bg: '#19131f', panel: '#241c2f', accent: '#bfa0ed', warn: '#dcaa70', ok: '#84caa4', sel: '#382746', 'accent-ink': '#21112f' },
  },
  synthwave: {
    light: { ink: '#291044', muted: '#735080', line: '#dfc4ed', bg: '#fff5ff', panel: '#f6e6ff', accent: '#ac167d', warn: '#8d4710', ok: '#176b60', sel: '#ebd4ff', 'accent-ink': '#ffffff' },
    dim: { ink: '#f8e6ff', muted: '#c7a4d6', line: '#704285', bg: '#30203f', panel: '#3c284f', accent: '#ff85de', warn: '#ffbc76', ok: '#61e1d3', sel: '#56315f', 'accent-ink': '#340b2a' },
    dark: { ink: '#f9eaff', muted: '#bd9bd0', line: '#57336d', bg: '#180d28', panel: '#26133a', accent: '#ff73d5', warn: '#ffb86c', ok: '#52e0d1', sel: '#42204f', 'accent-ink': '#300822' },
  },
  terminal: {
    light: { ink: '#12351c', muted: '#446649', line: '#bcd7b8', bg: '#f1f9e9', panel: '#e4f1da', accent: '#246336', warn: '#84581d', ok: '#246336', sel: '#d0e6c3', 'accent-ink': '#ffffff' },
    dim: { ink: '#c8efb8', muted: '#99bc8f', line: '#46653e', bg: '#202d1c', panel: '#293922', accent: '#a4ed7c', warn: '#eac077', ok: '#a4ed7c', sel: '#324627', 'accent-ink': '#14240b' },
    dark: { ink: '#bafa9b', muted: '#89b879', line: '#33552b', bg: '#091207', panel: '#12200e', accent: '#83f052', warn: '#e9c46a', ok: '#83f052', sel: '#20371a', 'accent-ink': '#0e2007' },
  },
  bubblegum: {
    light: { ink: '#461c43', muted: '#815674', line: '#edc3df', bg: '#fff2fa', panel: '#ffe0f1', accent: '#9a288e', warn: '#8a481b', ok: '#20624f', sel: '#f9d7ef', 'accent-ink': '#ffffff' },
    dim: { ink: '#ffe5f5', muted: '#d5a5c4', line: '#794d6c', bg: '#3b2535', panel: '#492e42', accent: '#ff9de1', warn: '#ffc285', ok: '#89dfc0', sel: '#58334e', 'accent-ink': '#3b1230' },
    dark: { ink: '#ffe7f6', muted: '#ce9cbe', line: '#643c57', bg: '#250e20', panel: '#35172d', accent: '#ff8bd8', warn: '#ffb879', ok: '#79d9b6', sel: '#4e2544', 'accent-ink': '#330d29' },
  },
  radioactive: {
    light: { ink: '#283214', muted: '#5c6838', line: '#cbd994', bg: '#f6ffdb', panel: '#e8f6b5', accent: '#7038a3', warn: '#8a5118', ok: '#3a6b22', sel: '#daeaa1', 'accent-ink': '#ffffff' },
    dim: { ink: '#ecffb4', muted: '#b3c785', line: '#56663b', bg: '#28301d', panel: '#333d25', accent: '#d7a0ff', warn: '#ffc477', ok: '#b6ed5b', sel: '#3d472c', 'accent-ink': '#2b103e' },
    dark: { ink: '#e5ffa0', muted: '#a8c177', line: '#45552b', bg: '#121a08', panel: '#1e2910', accent: '#d69aff', warn: '#ffbd69', ok: '#b0f047', sel: '#303e1c', 'accent-ink': '#260d39' },
  },
};
const appearanceQuery = window.matchMedia('(prefers-color-scheme: dark)');
let themeSettings = readThemeSettings();

function readThemeSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(THEME_STORAGE_KEY) || '{}');
    return {
      appearance: APPEARANCES.includes(saved.appearance) ? saved.appearance : DEFAULT_THEME.appearance,
      colors: Object.hasOwn(THEME_PALETTES, saved.colors) ? saved.colors : DEFAULT_THEME.colors,
    };
  } catch (error) {
    return { ...DEFAULT_THEME };
  }
}

function applyThemeSettings(settings, persist = false) {
  themeSettings = settings;
  const mode = settings.appearance === 'system'
    ? (appearanceQuery.matches ? 'dark' : 'light')
    : settings.appearance;
  const root = document.documentElement;
  root.dataset.appearance = settings.appearance;
  root.dataset.colorTheme = settings.colors;
  root.style.colorScheme = mode === 'light' ? 'light' : 'dark';
  Object.entries(THEME_PALETTES[settings.colors][mode]).forEach(([name, value]) => {
    root.style.setProperty(`--${name}`, value);
  });
  // Canvas features repaint from the same colors after styles are applied.
  requestAnimationFrame(() => document.dispatchEvent(new Event('doxograph:appearance')));
  if (persist) {
    try { localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify(settings)); } catch (error) { /* preference remains for this page */ }
  }
}

applyThemeSettings(themeSettings);
appearanceQuery.addEventListener('change', () => {
  if (themeSettings.appearance === 'system') applyThemeSettings(themeSettings);
});
