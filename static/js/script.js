




const textInput = document.getElementById("text-input");
const actionMode = document.getElementById("action-mode");
const submitBtn = document.getElementById("submit-btn");
const historyList = document.getElementById("history-list");
const clearHistoryBtn = document.getElementById("clear-history");
const clipboardManagerSection = document.getElementById(
  "clipboard-manager-section",
);
const copiedTextSection = document.getElementById("copied-text-section");
const copiedTextList = document.getElementById("copied-text-list");
const clearCopiedTextBtn = document.getElementById("clear-copied-text");
const clipboardManagerBtn = document.getElementById("clipboard-manager-btn");
const copiedTextBtn = document.getElementById("copied-text-btn");
const errorMessage = document.createElement("p");
errorMessage.className = "error";
clipboardManagerSection.appendChild(errorMessage);

const username = document.querySelector("header p").textContent.split(": ")[1];


let pollTimer = null;
const POLL_INTERVAL = 5000; 
const cachedVersions = {
  copied: "",
  submitted: "",
  clipboard: "",
};
let lastCopiedTextHash = "";
let activeSection = "clipboard-manager"; 





async function smartPoll() {
  try {
    const response = await fetch(`/api/poll/${encodeURIComponent(username)}`, {
      credentials: "include",
    });
    if (!response.ok) return;

    const data = await response.json();
    if (data.status !== "ok" || !data.v) return;

    const v = data.v;

    
    const copiedChanged = v.copied !== cachedVersions.copied;
    const submittedChanged = v.submitted !== cachedVersions.submitted;

    
    cachedVersions.copied = v.copied;
    cachedVersions.submitted = v.submitted;
    cachedVersions.clipboard = v.clipboard;

    
    if (copiedChanged && activeSection === "copied-text") {
      await loadCopiedText();
    }
    if (submittedChanged && activeSection === "clipboard-manager") {
      await loadSubmittedTextHistory();
    }
  } catch (err) {
    
    console.debug("Poll check failed:", err.message);
  }
}

function startPolling() {
  stopPolling();
  
  smartPoll();
  pollTimer = setInterval(smartPoll, POLL_INTERVAL);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}





function showClipboardManager() {
  activeSection = "clipboard-manager";
  clipboardManagerSection.style.display = "block";
  copiedTextSection.style.display = "none";
  clipboardManagerBtn.classList.add("active");
  copiedTextBtn.classList.remove("active");
  
  loadSubmittedTextHistory();
}

function showCopiedText() {
  activeSection = "copied-text";
  clipboardManagerSection.style.display = "none";
  copiedTextSection.style.display = "block";
  clipboardManagerBtn.classList.remove("active");
  copiedTextBtn.classList.add("active");
  
  cachedVersions.copied = "";
  loadCopiedText();
}

clipboardManagerBtn.addEventListener("click", showClipboardManager);
copiedTextBtn.addEventListener("click", showCopiedText);





async function loadSubmittedTextHistory() {
  try {
    const response = await fetch(
      `/api/submitted_text_history/${encodeURIComponent(username)}`,
      {
        credentials: "include",
      },
    );
    if (!response.ok) {
      throw new Error(`HTTP error! Status: ${response.status}`);
    }
    const data = await response.json();
    if (data.status === "success") {
      historyList.innerHTML = "";
      const submittedHistory = data.submitted_text_history || [];
      if (submittedHistory.length === 0) {
        const emptyItem = document.createElement("li");
        emptyItem.textContent = "No submitted text yet...";
        emptyItem.className = "history-empty text-gray-500";
        historyList.appendChild(emptyItem);
      } else {
        submittedHistory.forEach((item) => addToSubmittedTextHistory(item));
      }
    } else {
      throw new Error(data.message || "Failed to load submitted text history");
    }
  } catch (error) {
    console.error("Error loading submitted text history:", error);
    errorMessage.textContent = `Error loading submitted text history: ${error.message}`;
  }
}

async function loadCopiedText() {
  try {
    const response = await fetch(
      `/api/copied_text_history/${encodeURIComponent(username)}`,
      {
        credentials: "include",
      },
    );
    if (!response.ok) {
      throw new Error(`HTTP error! Status: ${response.status}`);
    }
    const data = await response.json();
    if (data.status === "success") {
      const copiedTextHistory = data.copied_text_history || [];
      const currentHash = copiedTextHistory.join("|");
      if (currentHash !== lastCopiedTextHash) {
        lastCopiedTextHash = currentHash;
        copiedTextList.innerHTML = "";
        if (copiedTextHistory.length === 0) {
          const emptyItem = document.createElement("li");
          emptyItem.textContent = "No copied text yet...";
          emptyItem.className = "history-empty text-gray-500";
          copiedTextList.appendChild(emptyItem);
        } else {
          copiedTextHistory.forEach((item) => addToCopiedText(item));
        }
      }
    } else {
      throw new Error(data.message || "Failed to load copied text");
    }
  } catch (error) {
    console.error("Error loading copied text history:", error);
    errorMessage.textContent = `Error loading copied text: ${error.message}`;
  }
}





function addToSubmittedTextHistory(text) {
  
  const existingItems = historyList.getElementsByTagName("li");
  for (let item of existingItems) {
    if (
      item.querySelector("span") &&
      item.querySelector("span").textContent === text
    ) {
      return;
    }
  }

  const listItem = document.createElement("li");
  listItem.className = "history-item";

  const textSpan = document.createElement("span");
  textSpan.textContent = text;
  listItem.appendChild(textSpan);

  const copyBtn = document.createElement("button");
  copyBtn.textContent = "Copy";
  copyBtn.className = "copy-btn";
  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(text).then(() => {
      alert("Text copied to clipboard!");
    });
  });
  listItem.appendChild(copyBtn);

  const deleteBtn = document.createElement("button");
  deleteBtn.textContent = "\u2715";
  deleteBtn.className = "delete-btn";
  deleteBtn.addEventListener("click", async () => {
    listItem.remove();
    try {
      const response = await fetch(
        `/api/delete_submitted_text/${encodeURIComponent(username)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ text }),
        },
      );
      if (!response.ok) {
        throw new Error(`HTTP error! Status: ${response.status}`);
      }
    } catch (error) {
      console.error("Error deleting submitted text item:", error);
      errorMessage.textContent = `Error deleting submitted text item: ${error.message}`;
    }
  });
  listItem.appendChild(deleteBtn);

  const emptyItem = historyList.querySelector(".text-gray-500");
  if (emptyItem) emptyItem.remove();

  historyList.insertBefore(listItem, historyList.firstChild);
}

function addToCopiedText(text) {
  
  const existingItems = copiedTextList.getElementsByTagName("li");
  for (let item of existingItems) {
    if (
      item.querySelector("span") &&
      item.querySelector("span").textContent === text
    ) {
      copiedTextList.removeChild(item);
      break;
    }
  }

  const listItem = document.createElement("li");
  listItem.className = "history-item";

  const textSpan = document.createElement("span");
  textSpan.textContent = text;
  listItem.appendChild(textSpan);

  const copyBtn = document.createElement("button");
  copyBtn.textContent = "Copy";
  copyBtn.className = "copy-btn";
  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(text).then(() => {
      alert("Text copied to clipboard!");
    });
  });
  listItem.appendChild(copyBtn);

  const deleteBtn = document.createElement("button");
  deleteBtn.textContent = "\u2715";
  deleteBtn.className = "delete-btn";
  deleteBtn.addEventListener("click", async () => {
    listItem.remove();
    try {
      const response = await fetch(
        `/api/delete_copied_text/${encodeURIComponent(username)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ text }),
        },
      );
      if (!response.ok) {
        throw new Error(`HTTP error! Status: ${response.status}`);
      }
    } catch (error) {
      console.error("Error deleting copied text item:", error);
      errorMessage.textContent = `Error deleting copied text item: ${error.message}`;
    }
  });
  listItem.appendChild(deleteBtn);

  const emptyItem = copiedTextList.querySelector(".text-gray-500");
  if (emptyItem) emptyItem.remove();

  copiedTextList.insertBefore(listItem, copiedTextList.firstChild);
}





function renderEmpty(list, message) {
  list.innerHTML = "";
  const emptyItem = document.createElement("li");
  emptyItem.textContent = message;
  emptyItem.className = "history-empty text-gray-500";
  list.appendChild(emptyItem);
}

clearHistoryBtn.addEventListener("click", async () => {
  renderEmpty(historyList, "No submitted text yet...");
  try {
    const response = await fetch(
      `/api/clear_submitted_text/${encodeURIComponent(username)}`,
      {
        method: "POST",
        credentials: "include",
      },
    );
    if (!response.ok) {
      throw new Error(`HTTP error! Status: ${response.status}`);
    }
    const data = await response.json();
    if (data.status !== "success") {
      throw new Error(data.message || "Failed to clear submitted text history");
    }
    alert("Submitted text history cleared!");
  } catch (error) {
    console.error("Error clearing submitted text history:", error);
    errorMessage.textContent = `Error clearing submitted text history: ${error.message}`;
  }
});

clearCopiedTextBtn.addEventListener("click", async () => {
  renderEmpty(copiedTextList, "No copied text yet...");
  lastCopiedTextHash = "";
  try {
    const response = await fetch(
      `/api/clear_copied_text/${encodeURIComponent(username)}`,
      {
        method: "POST",
        credentials: "include",
      },
    );
    if (!response.ok) {
      throw new Error(`HTTP error! Status: ${response.status}`);
    }
    const data = await response.json();
    if (data.status !== "success") {
      throw new Error(data.message || "Failed to clear copied text history");
    }
    alert("Copied text history cleared!");
  } catch (error) {
    console.error("Error clearing copied text history:", error);
    errorMessage.textContent = `Error clearing copied text history: ${error.message}`;
  }
});





submitBtn.addEventListener("click", async () => {
  const text = textInput.value.trim();
  const mode = actionMode.value;

  if (!text) {
    alert("Please enter some text!");
    return;
  }

  errorMessage.textContent = "";

  try {
    if (mode === "copy-to-clipboard" || mode === "both") {
      const clipboardResponse = await fetch(
        `/api/submit_to_clipboard/${encodeURIComponent(username)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ text }),
        },
      );
      if (!clipboardResponse.ok) {
        const errorData = await clipboardResponse.json();
        throw new Error(
          errorData.message ||
            `HTTP error! Status: ${clipboardResponse.status}`,
        );
      }
      const clipboardData = await clipboardResponse.json();
      if (clipboardData.status !== "success") {
        throw new Error(clipboardData.message || "Failed to send to clipboard");
      }
      alert("Text sent to system clipboard! It will be copied shortly.");
    }

    if (mode === "add-to-history" || mode === "both") {
      const historyResponse = await fetch(
        `/api/submit_submitted_text/${encodeURIComponent(username)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ text }),
        },
      );
      if (!historyResponse.ok) {
        const errorData = await historyResponse.json();
        throw new Error(
          errorData.message || `HTTP error! Status: ${historyResponse.status}`,
        );
      }
      const historyData = await historyResponse.json();
      if (historyData.status !== "success") {
        throw new Error(
          historyData.message || "Failed to add to submitted text history",
        );
      }
      addToSubmittedTextHistory(text);
      alert("Text added to history!");
    }

    textInput.value = "";
  } catch (error) {
    console.error("Error submitting text:", error);
    errorMessage.textContent = `Error submitting text: ${error.message}`;
  }
});





document.addEventListener("DOMContentLoaded", () => {
  showClipboardManager();
  startPolling();
});


document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPolling();
  } else {
    
    cachedVersions.copied = "";
    cachedVersions.submitted = "";
    cachedVersions.clipboard = "";
    startPolling();
  }
});
