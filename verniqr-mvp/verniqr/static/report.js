"use strict";
const form = document.getElementById("report-form");
if (form) {
  const message = document.getElementById("message");
  const error = document.getElementById("form-error");
  const button = document.getElementById("submit-button");
  message.addEventListener("input", () => {
    document.getElementById("char-count").textContent = String(message.value.length);
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (button.disabled) return;
    error.hidden = true;
    const text = message.value.trim();
    if (text.length < 10 || text.length > 1000) {
      error.textContent = "Сообщение должно содержать от 10 до 1000 символов.";
      error.hidden = false;
      return;
    }
    button.disabled = true;
    button.textContent = "Сохраняем…";
    try {
      if (form.dataset.demo === "true") {
        document.getElementById("success-text").textContent = "Это пример: настоящее сообщение не отправлено. На активной метке его получит владелец вещи.";
      } else {
        const response = await fetch(`/api/reports/${encodeURIComponent(form.dataset.itemId)}`, {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Form-Token": form.dataset.token},
          body: JSON.stringify({message: text, finder_contact: document.getElementById("finder-contact").value.trim() || null}),
          signal: AbortSignal.timeout(20000)
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.message || "Не удалось сохранить сообщение.");
      }
      document.getElementById("form-section").hidden = true;
      document.getElementById("success-section").hidden = false;
    } catch (failure) {
      error.textContent = failure.name === "TimeoutError" || failure.name === "TypeError"
        ? "Не удалось получить подтверждение. Проверьте связь и повторите отправку: дубликат не появится."
        : failure.message;
      error.hidden = false;
      button.disabled = false;
      button.textContent = form.dataset.demo === "true" ? "Проверить пример" : "Повторить отправку";
    }
  });
}
