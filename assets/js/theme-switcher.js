(function () {
  "use strict";

  var STORAGE_KEY = "bootstrap-template-theme";
  var root = document.documentElement;

  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    localStorage.setItem(STORAGE_KEY, theme);
    document.querySelectorAll("[data-theme-label]").forEach(function (el) {
      el.textContent = el.getAttribute("data-theme-label-" + theme) || theme;
    });
    document.querySelectorAll(".theme-option").forEach(function (el) {
      el.classList.toggle("active", el.getAttribute("data-theme-value") === theme);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var saved = localStorage.getItem(STORAGE_KEY) || "aero-blue";
    applyTheme(saved);

    document.querySelectorAll(".theme-option").forEach(function (el) {
      el.addEventListener("click", function (e) {
        e.preventDefault();
        applyTheme(el.getAttribute("data-theme-value"));
      });
    });

    var tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]');
    tooltipTriggerList.forEach(function (el) {
      new bootstrap.Tooltip(el);
    });
  });
})();
