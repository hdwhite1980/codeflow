const { getSupabase } = require('../server/config/database');
const jwt = require('jsonwebtoken');
const multer = require('multer');

const authenticateToken = (req) => {
  const authHeader = req.headers.authorization;
  const token = authHeader && authHeader.split(' ')[1];
  
  if (!token) {
    throw new Error('Access token required');
  }

  return jwt.verify(token, process.env.JWT_SECRET);
};

module.exports = async function handler(req, res) {
  // Enable CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  
  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  try {
    const user = authenticateToken(req);
    const supabase = getSupabase();

    if (req.method === 'GET') {
      const { data: projects, error } = await supabase
        .from('projects')
        .select(`
          *,
          code_files:code_files(count),
          analyses:analysis_results(count)
        `)
        .eq('user_id', user.userId)
        .order('created_at', { ascending: false });

      if (error) {
        return res.status(500).json({ error: error.message });
      }

      res.json({
        success: true,
        projects: projects.map(project => ({
          ...project,
          fileCount: project.code_files?.[0]?.count || 0,
          analysisCount: project.analyses?.[0]?.count || 0
        }))
      });

    } else if (req.method === 'POST') {
      const { name, description, repositoryUrl } = req.body;

      if (!name) {
        return res.status(400).json({ error: 'Project name is required' });
      }

      const { data: project, error } = await supabase
        .from('projects')
        .insert([
          {
            name,
            description,
            repository_url: repositoryUrl,
            user_id: user.userId,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString()
          }
        ])
        .select()
        .single();

      if (error) {
        return res.status(400).json({ error: error.message });
      }

      res.json({
        success: true,
        project
      });

    } else if (req.method === 'PUT') {
      const { id, name, description, repositoryUrl } = req.body;

      if (!id) {
        return res.status(400).json({ error: 'Project ID is required' });
      }

      const { data: project, error } = await supabase
        .from('projects')
        .update({
          name,
          description,
          repository_url: repositoryUrl,
          updated_at: new Date().toISOString()
        })
        .eq('id', id)
        .eq('user_id', user.userId)
        .select()
        .single();

      if (error) {
        return res.status(400).json({ error: error.message });
      }

      if (!project) {
        return res.status(404).json({ error: 'Project not found or access denied' });
      }

      res.json({
        success: true,
        project
      });

    } else if (req.method === 'DELETE') {
      const { id } = req.query;

      if (!id) {
        return res.status(400).json({ error: 'Project ID is required' });
      }

      const { error } = await supabase
        .from('projects')
        .delete()
        .eq('id', id)
        .eq('user_id', user.userId);

      if (error) {
        return res.status(400).json({ error: error.message });
      }

      res.json({
        success: true,
        message: 'Project deleted successfully'
      });

    } else {
      res.status(405).json({ error: 'Method not allowed' });
    }
  } catch (error) {
    res.status(401).json({ error: error.message });
  }
}