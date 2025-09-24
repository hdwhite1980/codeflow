const { createClient } = require('@supabase/supabase-js');

// Initialize Supabase client
function getSupabaseClient() {
  const supabaseUrl = process.env.SUPABASE_URL;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  
  if (!supabaseUrl || !supabaseServiceKey || 
      supabaseUrl.includes('your_supabase_project_url_here') ||
      supabaseServiceKey.includes('your_service_role_key_here')) {
    throw new Error('Production configuration required: Supabase credentials must be properly configured in environment variables. Contact your system administrator.');
  }
  
  return createClient(supabaseUrl, supabaseServiceKey, {
    auth: {
      autoRefreshToken: false,
      persistSession: false
    }
  });
}

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
      const supabase = getSupabaseClient();

      if (action === 'register') {
        if (!email || !password || !name) {
          return res.status(400).json({ error: 'Email, password, and name are required' });
        }

        // Create user account
        const { data: authData, error: authError } = await supabase.auth.admin.createUser({
          email,
          password,
          email_confirm: true,
          user_metadata: {
            name: name
          }
        });

        if (authError) {
          return res.status(400).json({ error: authError.message });
        }

        // Create user profile
        const { error: profileError } = await supabase
          .from('profiles')
          .insert([
            {
              id: authData.user.id,
              email: authData.user.email,
              name: name,
              created_at: new Date().toISOString()
            }
          ]);

        if (profileError) {
          console.warn('Profile creation failed:', profileError.message);
        }

        return res.status(200).json({
          user: {
            id: authData.user.id,
            email: authData.user.email,
            name: name,
            created_at: authData.user.created_at
          },
          session: authData.session
        });

      } else if (action === 'login') {
        if (!email || !password) {
          return res.status(400).json({ error: 'Email and password are required' });
        }

        // Sign in user
        const { data: authData, error: authError } = await supabase.auth.signInWithPassword({
          email,
          password
        });

        if (authError) {
          return res.status(401).json({ error: authError.message });
        }

        // Get user profile
        const { data: profile } = await supabase
          .from('profiles')
          .select('name')
          .eq('id', authData.user.id)
          .single();

        return res.status(200).json({
          user: {
            id: authData.user.id,
            email: authData.user.email,
            name: profile?.name || authData.user.email.split('@')[0],
            created_at: authData.user.created_at
          },
          session: authData.session
        });

      } else {
        return res.status(400).json({ error: 'Invalid action. Use "login" or "register"' });
      }

    } catch (error) {
      console.error('Auth error:', error);
      return res.status(500).json({ 
        error: error.message || 'Authentication failed',
        details: process.env.NODE_ENV === 'development' ? error.stack : undefined
      });
    }

  } else if (req.method === 'GET') {
    // Verify token endpoint
    try {
      const authorization = req.headers.authorization;
      if (!authorization || !authorization.startsWith('Bearer ')) {
        return res.status(401).json({ error: 'Missing or invalid authorization header' });
      }

      const token = authorization.split(' ')[1];
      const supabase = getSupabaseClient();

      const { data: { user }, error } = await supabase.auth.getUser(token);

      if (error || !user) {
        return res.status(401).json({ error: 'Invalid token' });
      }

      // Get user profile
      const { data: profile } = await supabase
        .from('profiles')
        .select('name')
        .eq('id', user.id)
        .single();

      return res.status(200).json({
        user: {
          id: user.id,
          email: user.email,
          name: profile?.name || user.email.split('@')[0],
          created_at: user.created_at
        }
      });

    } catch (error) {
      console.error('Token verification error:', error);
      return res.status(500).json({ 
        error: 'Token verification failed',
        details: process.env.NODE_ENV === 'development' ? error.stack : undefined
      });
    }

  } else {
    return res.status(405).json({ error: 'Method not allowed. Use POST or GET.' });
  }
};