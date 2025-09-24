module.exports = (req, res) => {
  res.writeHead(302, { Location: '/app.html' });
  res.end();
};