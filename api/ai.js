import { createClient } from '@supabase/supabase-js';
import jwt from 'jsonwebtoken';

// Initialize Supabase client  
const getSupabase = () => {
  const supabaseUrl = process.env.SUPABASE_URL;
  const supabaseKey = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_ANON_KEY;
  
  if (!supabaseUrl || !supabaseKey) {
    throw new Error('Missing Supabase configuration');
  }
  
  return createClient(supabaseUrl, supabaseKey);
};

// Simple AI service functions
const queryAI = async (provider, model, messages) => {
  // For now, return a mock response
  // This would integrate with actual AI services when API keys are configured
  return {
    response: `Mock ${provider} ${model} response: I received ${messages.length} messages. This is a placeholder response until API keys are configured.`,
    usage: {
      prompt_tokens: 100,
      completion_tokens: 50,
      total_tokens: 150
    }
  };
};

const authenticateToken = (req) => {
  const authHeader = req.headers.authorization;
  const token = authHeader && authHeader.split(' ')[1];
  
  if (!token) {
    throw new Error('Access token required');
  }

  return jwt.verify(token, process.env.JWT_SECRET);
};

export default async function handler(req, res) {
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

    if (req.method === 'POST') {
      const { action, provider, model, message, projectId, sessionId, context } = req.body;

      if (action === 'create-session') {
        // Create new AI session
        const { data: session, error } = await supabase
          .from('ai_sessions')
          .insert([
            {
              user_id: user.userId,
              project_id: projectId,
              provider: provider || 'openai',
              model: model || 'gpt-4',
              messages: [],
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
          session
        });

      } else if (action === 'send-message') {
        if (!sessionId || !message) {
          return res.status(400).json({ error: 'Session ID and message are required' });
        }

        // Get session
        const { data: session, error: sessionError } = await supabase
          .from('ai_sessions')
          .select('*')
          .eq('id', sessionId)
          .eq('user_id', user.userId)
          .single();

        if (sessionError || !session) {
          return res.status(404).json({ error: 'Session not found or access denied' });
        }

        // Add user message
        const messages = [...(session.messages || []), {
          role: 'user',
          content: message,
          timestamp: new Date().toISOString()
        }];

        // Query AI
        try {
          const aiResponse = await queryAI({
            provider: session.provider,
            model: session.model,
            messages: messages.map(m => ({ role: m.role, content: m.content })),
            context
          });

          // Add AI response
          messages.push({
            role: 'assistant',
            content: aiResponse.content,
            timestamp: new Date().toISOString()
          });

          // Update session
          const { data: updatedSession, error: updateError } = await supabase
            .from('ai_sessions')
            .update({
              messages,
              tokens_used: (session.tokens_used || 0) + (aiResponse.tokensUsed || 0),
              cost: parseFloat((parseFloat(session.cost || 0) + parseFloat(aiResponse.cost || 0)).toFixed(4)),
              updated_at: new Date().toISOString()
            })
            .eq('id', sessionId)
            .select()
            .single();

          if (updateError) {
            return res.status(400).json({ error: updateError.message });
          }

          res.json({
            success: true,
            response: aiResponse.content,
            session: updatedSession,
            tokensUsed: aiResponse.tokensUsed,
            cost: aiResponse.cost
          });

        } catch (aiError) {
          res.status(500).json({ error: `AI service error: ${aiError.message}` });
        }

      } else {
        res.status(400).json({ error: 'Invalid action' });
      }

    } else if (req.method === 'GET') {
      const { projectId, sessionId } = req.query;

      if (sessionId) {
        // Get specific session
        const { data: session, error } = await supabase
          .from('ai_sessions')
          .select('*')
          .eq('id', sessionId)
          .eq('user_id', user.userId)
          .single();

        if (error) {
          return res.status(404).json({ error: 'Session not found or access denied' });
        }

        res.json({
          success: true,
          session
        });

      } else if (projectId) {
        // Get all sessions for project
        const { data: sessions, error } = await supabase
          .from('ai_sessions')
          .select('*')
          .eq('project_id', projectId)
          .eq('user_id', user.userId)
          .order('created_at', { ascending: false });

        if (error) {
          return res.status(500).json({ error: error.message });
        }

        res.json({
          success: true,
          sessions
        });

      } else {
        // Get all user sessions
        const { data: sessions, error } = await supabase
          .from('ai_sessions')
          .select('*')
          .eq('user_id', user.userId)
          .order('created_at', { ascending: false })
          .limit(50);

        if (error) {
          return res.status(500).json({ error: error.message });
        }

        res.json({
          success: true,
          sessions
        });
      }

    } else {
      res.status(405).json({ error: 'Method not allowed' });
    }
  } catch (error) {
    res.status(401).json({ error: error.message });
  }
}