// AI Data JSON download for metrics and test pages

document.addEventListener('DOMContentLoaded', function () {
    function getDownloadFilename(response) {
        const disposition = response.headers.get('Content-Disposition') || '';
        const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
        if (utf8Match && utf8Match[1]) {
            return decodeURIComponent(utf8Match[1]);
        }
        const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
        if (plainMatch && plainMatch[1]) {
            return plainMatch[1];
        }
        return 'tab-export.json';
    }

    async function downloadAiData(link) {
        link.classList.add('disabled');
        link.setAttribute('aria-disabled', 'true');
        try {
            const response = await fetch(link.href, {
                credentials: 'same-origin',
            });
            if (!response.ok) {
                throw new Error('Export failed (' + response.status + ')');
            }
            const url = window.URL.createObjectURL(await response.blob());
            const anchor = document.createElement('a');
            anchor.href = url;
            anchor.download = getDownloadFilename(response);
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
            window.URL.revokeObjectURL(url);
        } catch (error) {
            console.error('AI data export failed', error);
            window.location.assign(link.href);
        } finally {
            link.classList.remove('disabled');
            link.removeAttribute('aria-disabled');
        }
    }

    document.addEventListener('click', function (e) {
        const downloadBtn = e.target.closest('#export-button');
        if (!downloadBtn) {
            return;
        }
        e.preventDefault();
        if (!downloadBtn.classList.contains('disabled')) {
            void downloadAiData(downloadBtn);
        }
    });
});
