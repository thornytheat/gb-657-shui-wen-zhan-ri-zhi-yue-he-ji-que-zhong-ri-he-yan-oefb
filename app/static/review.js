// 跨站复核：按当前归档日值重跑，归档新结论；同源重跑会命中已有结论。
const state = { busy: false };

function notice(message, isError = false) {
  const box = document.getElementById("notice");
  if (!box) return;
  box.textContent = message;
  box.classList.toggle("is-error", Boolean(isError));
  box.hidden = false;
}

async function rerun(button) {
  if (state.busy) return;
  state.busy = true;
  const label = button.textContent;
  button.textContent = "复核中";
  button.disabled = true;
  try {
    const response = await fetch("/api/cross-review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error((payload.error || response.status) + " " + (payload.message || ""));
    }
    const summary = payload.summary || {};
    const parts = [
      payload.created ? "已归档 " + payload.review_id : "命中已有结论 " + payload.review_id,
      "一致 " + (summary.consistent || 0),
      "提示巡查 " + (summary.divergent || 0),
      "证据不足 " + (summary.insufficient || 0),
    ];
    if (payload.invalidated_ids && payload.invalidated_ids.length) {
      parts.push("失效旧结论 " + payload.invalidated_ids.join("、"));
    }
    notice(parts.join(" · "));
    window.setTimeout(() => window.location.reload(), 600);
  } catch (error) {
    notice(String(error.message || error), true);
  } finally {
    button.textContent = label;
    button.disabled = false;
    state.busy = false;
  }
}

const button = document.getElementById("review-button");
if (button) {
  button.addEventListener("click", () => rerun(button));
}
