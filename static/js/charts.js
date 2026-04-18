/**
 * SeeQL Chart.js helpers
 *
 * Provides a global SeeQL namespace with reusable chart utilities
 * styled to match the hand-drawn design system.
 */

window.SeeQL = window.SeeQL || {};

// Store chart instances for cleanup on re-render
SeeQL._charts = {};

/**
 * Default Chart.js options matching the hand-drawn aesthetic.
 */
SeeQL.defaultChartOptions = function () {
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
            intersect: false,
            mode: 'index',
        },
        plugins: {
            legend: {
                labels: {
                    font: { family: "'Patrick Hand', cursive", size: 14 },
                    color: '#2d2d2d',
                },
            },
            tooltip: {
                backgroundColor: '#fdfbf7',
                titleColor: '#2d2d2d',
                bodyColor: '#2d2d2d',
                borderColor: '#2d2d2d',
                borderWidth: 2,
                titleFont: { family: "'Kalam', cursive", size: 14, weight: 'bold' },
                bodyFont: { family: "'Patrick Hand', cursive", size: 13 },
                padding: 10,
                cornerRadius: 4,
                displayColors: true,
            },
        },
        scales: {
            x: {
                grid: {
                    color: 'rgba(229, 224, 216, 0.5)',
                    drawBorder: true,
                    borderDash: [4, 4],
                },
                ticks: {
                    font: { family: "'Patrick Hand', cursive", size: 12 },
                    color: '#2d2d2d',
                    maxTicksLimit: 8,
                },
            },
            y: {
                grid: {
                    color: 'rgba(229, 224, 216, 0.5)',
                    drawBorder: true,
                    borderDash: [4, 4],
                },
                ticks: {
                    font: { family: "'Patrick Hand', cursive", size: 12 },
                    color: '#2d2d2d',
                },
                beginAtZero: true,
            },
        },
    };
};

/**
 * Format ISO timestamp for chart labels.
 */
SeeQL.formatTime = function (isoString) {
    if (!isoString) return '';
    try {
        const d = new Date(isoString);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
        // Fallback: just show last 5 chars (HH:MM)
        return isoString.slice(-8, -3);
    }
};

/**
 * Create or replace a chart on a canvas element.
 */
SeeQL.createChart = function (canvasId, config) {
    // Destroy existing chart on same canvas
    if (SeeQL._charts[canvasId]) {
        SeeQL._charts[canvasId].destroy();
    }

    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;

    const chart = new Chart(ctx, config);
    SeeQL._charts[canvasId] = chart;
    return chart;
};

/**
 * Fetch JSON data and render a simple line chart.
 */
SeeQL.fetchAndChart = function (url, canvasId, opts) {
    fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (!Array.isArray(data) || data.length === 0) {
                // Show empty state inside the fixed-height container
                var ctx = document.getElementById(canvasId);
                if (ctx) {
                    var parent = ctx.parentElement;
                    parent.innerHTML = '<p style="font-family: Patrick Hand, cursive; text-align: center; padding-top: 80px; color: rgba(45,45,45,0.4);">No data yet</p>';
                }
                return;
            }

            SeeQL.createChart(canvasId, {
                type: 'line',
                data: {
                    labels: data.map(function (d) { return SeeQL.formatTime(d[opts.timeKey]); }),
                    datasets: [{
                        label: opts.label || 'Value',
                        data: data.map(function (d) { return d[opts.valueKey]; }),
                        borderColor: opts.color || '#2d5da1',
                        backgroundColor: (opts.color || '#2d5da1') + '1a',
                        borderWidth: 2.5,
                        tension: 0.3,
                        fill: true,
                        pointRadius: 1.5,
                        pointHoverRadius: 4,
                    }],
                },
                options: SeeQL.defaultChartOptions(),
            });
        })
        .catch(function (err) {
            console.error('Chart fetch error:', canvasId, err);
        });
};
