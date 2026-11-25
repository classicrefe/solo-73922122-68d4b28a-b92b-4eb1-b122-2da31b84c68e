(() => {
  const coverageInput = document.getElementById("buildings");
  const checkboxes = Array.from(
    document.querySelectorAll('#coverage-checkboxes input[type="checkbox"]')
  );

  if (coverageInput && checkboxes.length > 0) {
    const tokensFromInput = () =>
      coverageInput.value
        .split(/[,，;；、\s]+/)
        .map((token) => token.trim())
        .filter(Boolean);

    const syncCheckboxesFromInput = () => {
      const tokens = new Set(tokensFromInput());
      checkboxes.forEach((box) => {
        const code = box.dataset.code || "";
        box.checked = tokens.has(box.value) || (code !== "" && tokens.has(code));
      });
    };

    const syncInputFromCheckboxes = () => {
      const selected = checkboxes
        .filter((box) => box.checked)
        .map((box) => box.value);
      coverageInput.value = selected.join(", ");
    };

    checkboxes.forEach((box) => {
      box.addEventListener("change", syncInputFromCheckboxes);
    });
    coverageInput.addEventListener("input", syncCheckboxesFromInput);

    if (!coverageInput.value.trim() && checkboxes.some((box) => box.checked)) {
      syncInputFromCheckboxes();
    } else {
      syncCheckboxesFromInput();
    }
  }

  const varianceInputs = Array.from(
    document.querySelectorAll('#variance-grid input[name="variance"]')
  );
  const bulkVariance = document.getElementById("variances");

  if (varianceInputs.length > 0 && bulkVariance) {
    const syncBulkFromInputs = () => {
      const values = varianceInputs.map((input) => input.value.trim());
      let last = -1;
      values.forEach((value, index) => {
        if (value) last = index;
      });
      bulkVariance.value = values.slice(0, last + 1).join("\n");
    };

    const syncInputsFromBulk = () => {
      const values = bulkVariance.value
        .split(/[,，;；、\s]+/)
        .map((value) => value.trim())
        .filter(Boolean);
      varianceInputs.forEach((input, index) => {
        input.value = values[index] || "";
      });
    };

    varianceInputs.forEach((input) => {
      input.addEventListener("input", syncBulkFromInputs);
    });
    bulkVariance.addEventListener("input", syncInputsFromBulk);
  }

  document.querySelectorAll("form.delete-form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      const reportId = form.dataset.reportId || "";
      if (!window.confirm(`确认删除报告 ${reportId}？`)) {
        event.preventDefault();
      }
    });
  });
})();
