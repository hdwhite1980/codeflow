const fs = require('fs');
const path = require('path');

// Create public directory if it doesn't exist
if (!fs.existsSync('public')) {
  fs.mkdirSync('public');
}

// Copy files to public directory
fs.copyFileSync('index.html', path.join('public', 'index.html'));
fs.copyFileSync('codeflow-browser.js', path.join('public', 'codeflow-browser.js'));
fs.copyFileSync('favicon.svg', path.join('public', 'favicon.svg'));

// Also copy the original CodeFlow.jsx for reference
if (fs.existsSync('CodeFlow.jsx')) {
  fs.copyFileSync('CodeFlow.jsx', path.join('public', 'CodeFlow.jsx'));
}

console.log('Build completed successfully!');