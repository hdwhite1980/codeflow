const fs = require('fs');
const path = require('path');

// Create public directory if it doesn't exist
if (!fs.existsSync('public')) {
  fs.mkdirSync('public');
}

// Copy files to public directory
fs.copyFileSync('index.html', path.join('public', 'index.html'));
fs.copyFileSync('CodeFlow.jsx', path.join('public', 'CodeFlow.jsx'));

console.log('Build completed successfully!');