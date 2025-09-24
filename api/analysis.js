module.exports = async (req, res) => {
  // Enable CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  
  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  if (req.method === 'POST') {
    const { code, language = 'javascript', filename = 'code.js' } = req.body || {};

    if (!code || !code.trim()) {
      return res.status(400).json({ error: 'Code content is required' });
    }

    try {
      // Simple code analysis without external dependencies
      const lines = code.split('\n');
      const nonEmptyLines = lines.filter(line => line.trim().length > 0);
      
      // Count functions (simple regex patterns)
      const functionMatches = code.match(/function\s+\w+|=>\s*{|function\s*\(|\w+\s*:\s*function/g) || [];
      
      // Count variables (simple regex patterns)  
      const variableMatches = code.match(/\b(let|const|var)\s+\w+/g) || [];
      
      // Count complexity indicators
      const complexityMatches = code.match(/\b(if|while|for|switch|catch|else if)\s*\(/g) || [];
      
      const metrics = {
        lines: lines.length,
        nonEmptyLines: nonEmptyLines.length,
        characters: code.length,
        functions: functionMatches.length,
        variables: variableMatches.length,
        complexity: complexityMatches.length + 1 // Base complexity of 1
      };

      const suggestions = [
        'Consider adding more comments for better code documentation',
        'Break down complex functions into smaller, more manageable functions',
        'Add proper error handling and validation',
        'Consider using consistent naming conventions',
        'Add unit tests to ensure code reliability'
      ];

      return res.status(200).json({
        metrics,
        suggestions,
        filename,
        language,
        timestamp: new Date().toISOString()
      });

    } catch (error) {
      return res.status(500).json({ 
        error: 'Analysis failed', 
        message: error.message 
      });
    }
  } else {
    return res.status(405).json({ error: 'Method not allowed. Use POST.' });
  }
};