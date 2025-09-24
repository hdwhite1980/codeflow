module.exports = async (req, res) => {
  // Enable CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  
  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  if (req.method === 'POST') {
    const { action, email, password, name } = req.body || {};

    try {
      if (action === 'register') {
        // Mock registration
        const mockUser = {
          id: Date.now().toString(),
          email,
          name,
          created_at: new Date().toISOString()
        };

        const token = `token_${Date.now()}`;

        return res.status(200).json({
          token,
          user: mockUser
        });

      } else if (action === 'login') {
        // Mock login - accepts any email/password
        if (email && password) {
          const mockUser = {
            id: Date.now().toString(),
            email,
            name: email.split('@')[0],
            created_at: new Date().toISOString()
          };

          const token = `token_${Date.now()}`;

          return res.status(200).json({
            token,
            user: mockUser
          });
        } else {
          return res.status(401).json({ error: 'Email and password required' });
        }

      } else {
        return res.status(400).json({ error: 'Invalid action. Use "login" or "register"' });
      }
    } catch (error) {
      return res.status(500).json({ error: error.message || 'Authentication failed' });
    }
  } else {
    return res.status(405).json({ error: 'Method not allowed. Use POST.' });
  }
};