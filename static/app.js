const documentList = document.querySelector("#document-list");
const documentCount = document.querySelector("#document-count");
const questionForm = document.querySelector("#question-form");
const questionInput = document.querySelector("#question-input");
const answerPanel = document.querySelector("#answer-panel");
const welcome = document.querySelector("#welcome");
const loading = document.querySelector("#loading");
const errorBanner = document.querySelector("#error-banner");
const askButton = document.querySelector("#ask-button");
const noteDialog = document.querySelector("#note-dialog");
const noteTitle = document.querySelector("#note-title");
const notePreview = document.querySelector("#note-preview");
const noteStatus = document.querySelector("#note-status");
const confirmNoteButton = document.querySelector("#confirm-note");
const downloadNoteLink = document.querySelector("#download-note");
const documentUpload = document.querySelector("#document-upload");

let currentAnswer = null;
let currentQuestion = "";

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail || `Request failed (${response.status})`);
    error.payload = body;
    throw error;
  }
  return body;
}

function showError(message) {
  errorBanner.textContent = message;
  errorBanner.hidden = false;
}

function clearError() {
  errorBanner.textContent = "";
  errorBanner.hidden = true;
}

function renderDocuments(names) {
  documentList.replaceChildren();
  documentCount.textContent = String(names.length);
  if (names.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty-library";
    empty.textContent = "No documents found. Add .md, .rst, or .txt files to your documents folder.";
    documentList.append(empty);
    return;
  }
  for (const name of names) {
    const item = document.createElement("li");
    item.className = "document-item";
    const icon = document.createElement("span");
    icon.className = "file-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "▤";
    const label = document.createElement("span");
    label.textContent = name;
    label.className = "document-name";
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "remove-document";
    removeButton.textContent = "×";
    removeButton.setAttribute("aria-label", `Remove ${name}`);
    removeButton.title = `Remove ${name}`;
    removeButton.addEventListener("click", () => removeDocument(name, removeButton));
    item.append(icon, label, removeButton);
    documentList.append(item);
  }
}

async function removeDocument(name, button) {
  button.disabled = true;
  clearError();
  try {
    const response = await fetch(`/api/documents/${encodeURIComponent(name)}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `Remove failed (${response.status})`);
    }
    await loadDocuments();
  } catch (error) {
    button.disabled = false;
    showError(`Could not remove ${name}: ${error.message}`);
  }
}

function renderAnswer(result) {
  answerPanel.replaceChildren();
  const kicker = document.createElement("p");
  kicker.className = "answer-kicker";
  kicker.textContent = "Answer";
  const answer = document.createElement("p");
  answer.className = "answer-text";
  answer.textContent = result.answer;
  answerPanel.append(kicker, answer);

  renderActions(result.operations, result.timings);

  for (const warning of result.citation_warnings) {
    const warningBox = document.createElement("div");
    warningBox.className = "citation-warning";
    warningBox.textContent = `Citation check: ${warning}`;
    answerPanel.append(warningBox);
  }

  if (result.sources.length) {
    const sources = document.createElement("div");
    sources.className = "sources";
    sources.setAttribute("aria-label", "Sources");
    result.sources.forEach((source, index) => {
      const chip = document.createElement("span");
      chip.className = "source-chip";
      chip.textContent = `[${index + 1}] ${source}`;
      sources.append(chip);
    });
    answerPanel.append(sources);
  }

  if (result.evidence.length) {
    const evidence = document.createElement("details");
    evidence.className = "evidence";
    const summary = document.createElement("summary");
    summary.textContent = `Retrieved passages (${result.evidence.length})`;
    evidence.append(summary);
    const sourceNumbers = new Map(result.sources.map((source, index) => [source, index + 1]));
    for (const passage of result.evidence) {
      const card = document.createElement("article");
      card.className = "passage";
      const source = document.createElement("p");
      source.className = "passage-source";
      source.textContent = `[${sourceNumbers.get(passage.source) ?? "?"}] ${passage.source}`;
      const text = document.createElement("p");
      text.className = "passage-text";
      text.textContent = passage.text;
      card.append(source, text);
      evidence.append(card);
    }
    answerPanel.append(evidence);
  }

  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.className = "save-button";
  saveButton.textContent = "Save answer as a note";
  saveButton.addEventListener("click", openNotePreview);
  answerPanel.append(saveButton);
  currentAnswer = result;
  answerPanel.hidden = false;
  welcome.hidden = true;
}

function renderActions(operations = [], timings = {}) {
  const section = document.createElement("section");
  section.className = "actions-trace";
  const heading = document.createElement("h3");
  heading.className = "actions-heading";
  heading.textContent = "Actions taken";
  section.append(heading);

  if (timings.total_ms !== undefined) {
    const timingSummary = document.createElement("p");
    timingSummary.className = "actions-timing";
    timingSummary.textContent =
      `Planning ${Number(timings.planning_ms || 0).toFixed(1)} ms · ` +
      `Execution ${Number(timings.execution_ms || 0).toFixed(1)} ms · ` +
      `Total ${Number(timings.total_ms).toFixed(1)} ms`;
    section.append(timingSummary);
  }

  const list = document.createElement("ol");
  list.className = "action-list";
  for (const operation of operations) {
    const item = document.createElement("li");
    item.className = "action-item";
    const row = document.createElement("div");
    row.className = "action-row";
    const command = document.createElement("span");
    command.className = "action-command";
    command.textContent = operation.command;
    const status = document.createElement("span");
    status.className = `action-status status-${operation.status}`;
    status.textContent = operation.status;
    const duration = document.createElement("span");
    duration.className = "action-duration";
    duration.textContent = `${Number(operation.duration_ms || 0).toFixed(1)} ms`;
    row.append(command, status, duration);
    const feedback = document.createElement("p");
    feedback.className = "action-feedback";
    feedback.textContent = operation.feedback || "";
    item.append(row, feedback);
    list.append(item);
  }
  if (!operations.length) {
    const empty = document.createElement("li");
    empty.className = "action-empty";
    empty.textContent = "No actions completed.";
    list.append(empty);
  }
  section.append(list);
  answerPanel.append(section);
}

async function loadDocuments() {
  try {
    const result = await requestJson("/api/documents");
    renderDocuments(result.documents);
  } catch (error) {
    renderDocuments([]);
    showError(`Could not load documents: ${error.message}`);
  }
}

documentUpload.addEventListener("change", async () => {
  const files = Array.from(documentUpload.files || []);
  if (!files.length) return;
  clearError();
  for (const file of files) {
    const formData = new FormData();
    formData.append("file", file);
    try {
      const response = await fetch("/api/documents", {
        method: "POST",
        body: formData,
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `Upload failed (${response.status})`);
    } catch (error) {
      showError(`Could not upload ${file.name}: ${error.message}`);
    }
  }
  documentUpload.value = "";
  await loadDocuments();
});

questionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question) return;
  currentQuestion = question;
  clearError();
  loading.hidden = false;
  askButton.disabled = true;
  try {
    const result = await requestJson("/api/ask", {
      method: "POST",
      body: JSON.stringify({ question }),
    });
    renderAnswer(result);
    questionInput.value = "";
  } catch (error) {
    if (error.payload?.operations) {
      answerPanel.replaceChildren();
      renderActions(error.payload.operations, error.payload.timings);
      answerPanel.hidden = false;
      welcome.hidden = true;
    }
    showError(error.message);
  } finally {
    loading.hidden = true;
    askButton.disabled = false;
  }
});

document.querySelectorAll(".suggestion").forEach((button) => {
  button.addEventListener("click", () => {
    questionInput.value = button.textContent;
    questionInput.focus();
  });
});

async function openNotePreview() {
  if (!currentAnswer) return;
  clearError();
  noteStatus.textContent = "";
  downloadNoteLink.hidden = true;
  noteTitle.value = currentQuestion || "Saved answer";
  try {
    const result = await requestJson("/api/notes", {
      method: "POST",
      body: JSON.stringify({
        title: noteTitle.value,
        content: currentAnswer.answer,
        sources: currentAnswer.sources,
        confirmed: false,
      }),
    });
    notePreview.textContent = result.preview;
    downloadNoteLink.hidden = true;
    noteDialog.showModal();
  } catch (error) {
    showError(`Could not preview note: ${error.message}`);
  }
}

confirmNoteButton.addEventListener("click", async () => {
  if (!currentAnswer || !noteTitle.value.trim()) {
    noteStatus.textContent = "Enter a title for this note.";
    return;
  }
  confirmNoteButton.disabled = true;
  noteStatus.textContent = "Saving…";
  try {
    const result = await requestJson("/api/notes", {
      method: "POST",
      body: JSON.stringify({
        title: noteTitle.value.trim(),
        content: currentAnswer.answer,
        sources: currentAnswer.sources,
        confirmed: true,
      }),
    });
    notePreview.textContent = result.preview;
    noteStatus.textContent = result.saved
      ? `Saved as ${result.filename}. Use the download link below.`
      : "Note was not saved.";
    if (result.saved) {
      downloadNoteLink.href = result.download_url;
      downloadNoteLink.download = "";
      downloadNoteLink.hidden = false;
      confirmNoteButton.hidden = true;
    }
  } catch (error) {
    noteStatus.textContent = error.message;
  } finally {
    confirmNoteButton.disabled = false;
  }
});

function closeNoteDialog() {
  noteDialog.close();
  confirmNoteButton.hidden = false;
}

document.querySelector("#close-note").addEventListener("click", closeNoteDialog);
document.querySelector("#cancel-note").addEventListener("click", closeNoteDialog);
noteDialog.addEventListener("click", (event) => {
  if (event.target === noteDialog) closeNoteDialog();
});

loadDocuments();
