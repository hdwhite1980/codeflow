export default async function handler(req, res) {
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
        // Mock registration - in real app this would save to database
        const mockUser = {
          id: Date.now().toString(),
          email,
          name,
          created_at: new Date().toISOString()
        };

        // Mock JWT token
        const token = `mock_token_${Date.now()}`;

        res.json({
          token,
          user: mockUser
        });

      } else if (action === 'login') {
        // Mock login - in real app this would verify against database
        if (email && password) {
          const mockUser = {
            id: Date.now().toString(),
            email,
            name: email.split('@')[0],
            created_at: new Date().toISOString()
          };

          // Mock JWT token
          const token = `mock_token_${Date.now()}`;

          res.json({
            token,
            user: mockUser
          });
        } else {
          res.status(401).json({ error: 'Invalid credentials' });
        }

      } else {
        res.status(400).json({ error: 'Invalid action. Use "login" or "register"' });
      }
    } catch (error) {
      res.status(500).json({ error: error.message || 'Authentication failed' });
    }
  } else {
    res.status(405).json({ error: 'Method not allowed. Use POST.' });
  }
}