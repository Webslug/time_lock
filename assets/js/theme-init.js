// Applies the saved theme before first paint (an external file, so the page needs no inline script).
(function () {
  try {
    var saved = localStorage.getItem("bootstrap-template-theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  } catch (e) { /* storage blocked: keep the default theme */ }
})();
