// Base JavaScript functionality for the application

document.addEventListener('DOMContentLoaded', function () {
    function copyToClipboard(text) {
        try {
            const p = navigator.clipboard && navigator.clipboard.writeText(text);
            if (p && typeof p.catch === 'function') {
                void p.catch(function () {
                    void 0;
                });
            }
        } catch (ignored) {
            void ignored;
        }
    }

    document.addEventListener('click', function (e) {
        const agentUrlBtn = e.target.closest('[data-copy-agent-url]');
        if (agentUrlBtn) {
            e.preventDefault();
            const url = agentUrlBtn.getAttribute('data-copy-agent-url');
            if (!url) {
                return;
            }
            copyToClipboard(url);
            const label = agentUrlBtn.firstChild;
            const previousText = label.textContent;
            label.textContent = 'Copied Agent URL';
            setTimeout(function () {
                label.textContent = previousText;
            }, 1000);
            return;
        }

        const promptBtn = e.target.closest('[data-copy-prompt]');
        if (promptBtn) {
            if (promptBtn.disabled) {
                return;
            }
            const ta = document.getElementById(
                promptBtn.getAttribute('data-copy-prompt')
            );
            if (!ta || !ta.value) {
                return;
            }
            e.preventDefault();
            const idle = promptBtn.querySelector('.js-copy-prompt-idle');
            const done = promptBtn.querySelector('.js-copy-prompt-done');
            idle?.classList.add('d-none');
            done?.classList.remove('d-none');
            setTimeout(function () {
                idle?.classList.remove('d-none');
                done?.classList.add('d-none');
            }, 1000);
            copyToClipboard(ta.value);
            return;
        }

        const lineBtn = e.target.closest('.copy-btn[data-clipboard-text]');
        if (!lineBtn) {
            return;
        }
        e.preventDefault();
        const text = lineBtn.getAttribute('data-clipboard-text');
        const icon = lineBtn.querySelector('i');
        const prev = icon && icon.getAttribute('class');
        if (icon && prev) {
            icon.setAttribute('class', 'fa-solid fa-check');
            setTimeout(function () {
                icon.setAttribute('class', prev);
            }, 1000);
        }
        copyToClipboard(text);
    });

    document.querySelectorAll('[id^="details-"]').forEach(function (modal) {
        const copyAiBtn = modal.querySelector('[data-copy-prompt]');

        modal.addEventListener('show.bs.modal', function (event) {
            const modalBody = this.querySelector('.modal-body');
            if (modalBody.dataset.loaded === 'true') {
                return;
            }

            const opener = event.relatedTarget;
            const resultId = opener && opener.getAttribute('data-result-id');
            if (!resultId) {
                return;
            }

            fetch('/projects/_result-details/' + resultId)
                .then(function (r) {
                    return r.text();
                })
                .then(function (html) {
                    modalBody.innerHTML = html;
                    modalBody.dataset.loaded = 'true';
                    if (copyAiBtn) {
                        copyAiBtn.disabled = false;
                    }
                });
        });
    });
});
