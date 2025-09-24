const { getSupabase } = require('../server/config/database');

module.exports = async function handler(req, res) {
  // Enable CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  
  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  if (req.method === 'GET') {
    try {
      const supabase = getSupabase();
      
      // Test database connection
      const { data, error } = await supabase
        .from('users')
        .select('count')
        .limit(1);
      
      res.json({ 
        status: 'ok', 
        timestamp: new Date().toISOString(),
        version: '1.0.0',
        database: error ? 'disconnected' : 'connected'
      });
    } catch (error) {
      res.status(500).json({ 
        status: 'error', 
        message: error.message,
        timestamp: new Date().toISOString()
      });
    }
  } else {
    res.status(405).json({ error: 'Method not allowed' });
  }
}