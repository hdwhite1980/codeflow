const { createClient } = require('@supabase/supabase-js');

let supabase;

const initSupabase = () => {
  if (!supabase) {
    const supabaseUrl = process.env.SUPABASE_URL;
    const supabaseKey = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_ANON_KEY;
    
    if (!supabaseUrl || !supabaseKey) {
      throw new Error('Missing Supabase configuration. Please set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY environment variables.');
    }
    
    supabase = createClient(supabaseUrl, supabaseKey);
  }
  return supabase;
};

const getSupabase = () => {
  return initSupabase();
};

module.exports = { getSupabase, initSupabase };