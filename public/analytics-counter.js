const counter = document.querySelector("#visit-counter");

if (counter) {
  fetch("/api/analytics/visits", { headers: { Accept: "application/json" } })
    .then((response) => (response.ok ? response.json() : null))
    .then((data) => {
      if (!data || !Number.isSafeInteger(data.visits) || data.visits < 0) return;
      counter.textContent = `${data.visits} ${data.visits === 1 ? "visit" : "visits"}`;
      counter.hidden = false;
    })
    .catch(() => {});
}
