import { createClient } from '@supabase/supabase-js';
import jwt from 'jsonwebtoken';
import acorn from 'acorn';
import * as walk from 'acorn-walk';

// Initialize Supabase client  
const getSupabase = () => {
  const supabaseUrl = process.env.SUPABASE_URL;
  const supabaseKey = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_ANON_KEY;
  
  if (!supabaseUrl || !supabaseKey) {
    throw new Error('Missing Supabase configuration');
  }
  
  return createClient(supabaseUrl, supabaseKey);
};

// Simple code analysis functions
const analyzeCode = (code, language = 'javascript') => {
  const metrics = {
    lines: code.split('\n').length,
    characters: code.length,
    functions: 0,
    variables: 0,
    complexity: 1
  };

  if (language === 'javascript') {
    try {
      const ast = acorn.parse(code, { ecmaVersion: 2020, sourceType: 'module' });
      
      walk.simple(ast, {
        FunctionDeclaration: () => metrics.functions++,
        FunctionExpression: () => metrics.functions++,
        ArrowFunctionExpression: () => metrics.functions++,
        VariableDeclarator: () => metrics.variables++,
        IfStatement: () => metrics.complexity++,
        WhileStatement: () => metrics.complexity++,
        ForStatement: () => metrics.complexity++,
        SwitchCase: () => metrics.complexity++
      });
    } catch (error) {
      // If parsing fails, return basic metrics
      metrics.functions = (code.match(/function\s+\w+|=>\s*{|function\s*\(/g) || []).length;
      metrics.variables = (code.match(/\b(let|const|var)\s+\w+/g) || []).length;
      metrics.complexity = (code.match(/\b(if|while|for|switch)\s*\(/g) || []).length + 1;
    }
  }

  return {
    metrics,
    suggestions: [
      'Consider adding more comments for better code documentation',
      'Consider breaking down complex functions into smaller ones',
      'Ensure proper error handling is implemented'
    ]
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
      const { code, language, projectId, filename, analysisType } = req.body;

      if (!code) {
        return res.status(400).json({ error: 'Code content is required' });
      }

      let analysisResult;

      switch (analysisType) {
        case 'complexity':
          analysisResult = await analyzeComplexity(code, language);
          break;
        case 'security':
          analysisResult = await detectSecurityIssues(code, language);
          break;
        case 'full':
        default:
          analysisResult = await analyzeCode(code, language);
          break;
      }

      // Store analysis result in database if projectId is provided
      if (projectId) {
        // Verify user owns the project
        const { data: project } = await supabase
          .from('projects')
          .select('id')
          .eq('id', projectId)
          .eq('user_id', user.userId)
          .single();

        if (!project) {
          return res.status(403).json({ error: 'Project not found or access denied' });
        }

        // Store the analysis
        await supabase
          .from('analysis_results')
          .insert([
            {
              project_id: projectId,
              analysis_type: analysisType || 'full',
              results: analysisResult,
              score: analysisResult.overallScore || analysisResult.complexity?.score || 0,
              created_at: new Date().toISOString()
            }
          ]);

        // Store security vulnerabilities if any
        if (analysisResult.security && analysisResult.security.vulnerabilities) {
          const vulnerabilities = analysisResult.security.vulnerabilities.map(vuln => ({
            project_id: projectId,
            vulnerability_type: vuln.type,
            severity: vuln.severity,
            description: vuln.message,
            line_number: vuln.line,
            remediation: vuln.remediation,
            created_at: new Date().toISOString()
          }));

          if (vulnerabilities.length > 0) {
            await supabase
              .from('security_vulnerabilities')
              .insert(vulnerabilities);
          }
        }
      }

      res.json({
        success: true,
        analysis: analysisResult,
        timestamp: new Date().toISOString()
      });

    } else if (req.method === 'GET') {
      const { projectId } = req.query;

      if (!projectId) {
        return res.status(400).json({ error: 'Project ID is required' });
      }

      // Verify user owns the project
      const { data: project } = await supabase
        .from('projects')
        .select('id')
        .eq('id', projectId)
        .eq('user_id', user.userId)
        .single();

      if (!project) {
        return res.status(403).json({ error: 'Project not found or access denied' });
      }

      // Get analysis results
      const { data: analyses, error } = await supabase
        .from('analysis_results')
        .select('*')
        .eq('project_id', projectId)
        .order('created_at', { ascending: false });

      if (error) {
        return res.status(500).json({ error: error.message });
      }

      res.json({
        success: true,
        analyses,
        count: analyses.length
      });

    } else {
      res.status(405).json({ error: 'Method not allowed' });
    }
  } catch (error) {
    res.status(401).json({ error: error.message });
  }
}