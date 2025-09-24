module.exports = async (req, res) => {
  // Enable CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  
  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  if (req.method === 'POST') {
    const { code, language = 'javascript', filename = 'code.js', files } = req.body || {};

    if (!code && !files) {
      return res.status(400).json({ error: 'Code content or files array is required' });
    }

    try {
      // Advanced code structure analysis
      const analyzeCodeStructure = (sourceCode, fileName) => {
        const analysis = {
          filename: fileName,
          nodes: [],
          dependencies: [],
          exports: [],
          imports: [],
          functions: [],
          classes: [],
          constants: [],
          variables: []
        };

        // Extract imports
        const importRegex = /import\s+(?:(?:\{([^}]+)\})|([^,\s]+)|(\*\s+as\s+\w+))\s+from\s+['"](.*?)['"];?/g;
        let importMatch;
        while ((importMatch = importRegex.exec(sourceCode)) !== null) {
          const [, namedImports, defaultImport, namespaceImport, source] = importMatch;
          
          if (namedImports) {
            namedImports.split(',').forEach(imp => {
              const trimmed = imp.trim();
              analysis.imports.push({
                type: 'named',
                name: trimmed,
                source: source,
                line: sourceCode.substring(0, importMatch.index).split('\n').length
              });
            });
          }
          
          if (defaultImport) {
            analysis.imports.push({
              type: 'default',
              name: defaultImport.trim(),
              source: source,
              line: sourceCode.substring(0, importMatch.index).split('\n').length
            });
          }
          
          if (namespaceImport) {
            analysis.imports.push({
              type: 'namespace',
              name: namespaceImport.replace('* as ', '').trim(),
              source: source,
              line: sourceCode.substring(0, importMatch.index).split('\n').length
            });
          }
        }

        // Extract require statements
        const requireRegex = /(?:const|let|var)\s+(?:\{([^}]+)\}|(\w+))\s*=\s*require\s*\(\s*['"](.*?)['"][\s]*\)/g;
        let requireMatch;
        while ((requireMatch = requireRegex.exec(sourceCode)) !== null) {
          const [, destructured, variable, source] = requireMatch;
          
          if (destructured) {
            destructured.split(',').forEach(imp => {
              analysis.imports.push({
                type: 'require-destructured',
                name: imp.trim(),
                source: source,
                line: sourceCode.substring(0, requireMatch.index).split('\n').length
              });
            });
          } else if (variable) {
            analysis.imports.push({
              type: 'require',
              name: variable,
              source: source,
              line: sourceCode.substring(0, requireMatch.index).split('\n').length
            });
          }
        }

        // Extract exports
        const exportRegex = /export\s+(?:(default)\s+)?(?:(class|function|const|let|var)\s+)?(\w+)|export\s*\{([^}]+)\}/g;
        let exportMatch;
        while ((exportMatch = exportRegex.exec(sourceCode)) !== null) {
          const [, isDefault, type, name, namedExports] = exportMatch;
          
          if (namedExports) {
            namedExports.split(',').forEach(exp => {
              analysis.exports.push({
                type: 'named',
                name: exp.trim(),
                line: sourceCode.substring(0, exportMatch.index).split('\n').length
              });
            });
          } else if (name) {
            analysis.exports.push({
              type: isDefault ? 'default' : 'named',
              name: name,
              elementType: type,
              line: sourceCode.substring(0, exportMatch.index).split('\n').length
            });
          }
        }

        // Extract module.exports
        const moduleExportRegex = /module\.exports\s*=\s*(\w+|{[^}]*}|\([^)]*\)\s*=>\s*{)/g;
        let moduleExportMatch;
        while ((moduleExportMatch = moduleExportRegex.exec(sourceCode)) !== null) {
          analysis.exports.push({
            type: 'module.exports',
            name: 'default',
            line: sourceCode.substring(0, moduleExportMatch.index).split('\n').length
          });
        }

        // Extract functions
        const functionRegex = /(?:export\s+)?(async\s+)?(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>\s*{|(\w+)\s*:\s*(?:async\s+)?function)/g;
        let functionMatch;
        while ((functionMatch = functionRegex.exec(sourceCode)) !== null) {
          const [, isAsync, funcName1, funcName2, funcName3] = functionMatch;
          const funcName = funcName1 || funcName2 || funcName3;
          
          if (funcName) {
            analysis.functions.push({
              name: funcName,
              type: 'function',
              isAsync: !!isAsync,
              line: sourceCode.substring(0, functionMatch.index).split('\n').length
            });
          }
        }

        // Extract classes
        const classRegex = /(?:export\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?/g;
        let classMatch;
        while ((classMatch = classRegex.exec(sourceCode)) !== null) {
          const [, className, extendsClass] = classMatch;
          
          analysis.classes.push({
            name: className,
            type: 'class',
            extends: extendsClass,
            line: sourceCode.substring(0, classMatch.index).split('\n').length
          });
        }

        // Extract constants and variables
        const variableRegex = /(const|let|var)\s+([A-Z_][A-Z0-9_]*|[a-zA-Z_][a-zA-Z0-9_]*)\s*=/g;
        let variableMatch;
        while ((variableMatch = variableRegex.exec(sourceCode)) !== null) {
          const [, type, varName] = variableMatch;
          
          const isConstant = type === 'const' && /^[A-Z_][A-Z0-9_]*$/.test(varName);
          
          if (isConstant) {
            analysis.constants.push({
              name: varName,
              type: 'constant',
              line: sourceCode.substring(0, variableMatch.index).split('\n').length
            });
          } else {
            analysis.variables.push({
              name: varName,
              type: 'variable',
              variableType: type,
              line: sourceCode.substring(0, variableMatch.index).split('\n').length
            });
          }
        }

        // Create nodes for visualization
        analysis.nodes = [
          ...analysis.functions.map(f => ({
            id: `${fileName}:${f.name}`,
            name: f.name,
            type: 'function',
            file: fileName,
            line: f.line,
            data: f
          })),
          ...analysis.classes.map(c => ({
            id: `${fileName}:${c.name}`,
            name: c.name,
            type: 'class',
            file: fileName,
            line: c.line,
            data: c
          })),
          ...analysis.constants.map(c => ({
            id: `${fileName}:${c.name}`,
            name: c.name,
            type: 'constant',
            file: fileName,
            line: c.line,
            data: c
          }))
        ];

        // Create dependency relationships
        analysis.dependencies = analysis.imports.map(imp => ({
          from: fileName,
          to: imp.source,
          type: 'import',
          name: imp.name,
          importType: imp.type
        }));

        return analysis;
      };

      // Analyze single file or multiple files
      let analysisResult;
      
      if (files && Array.isArray(files)) {
        // Multi-file analysis for project mapping
        const fileAnalyses = files.map(file => 
          analyzeCodeStructure(file.content, file.name)
        );
        
        // Merge all analyses and create cross-file dependencies
        const allNodes = fileAnalyses.flatMap(analysis => analysis.nodes);
        const allDependencies = fileAnalyses.flatMap(analysis => analysis.dependencies);
        
        // Detect cross-file function calls
        const crossFileDependencies = [];
        fileAnalyses.forEach(analysis => {
          const sourceCode = files.find(f => f.name === analysis.filename).content;
          
          // Look for function calls to other files
          allNodes.forEach(node => {
            if (node.file !== analysis.filename) {
              const callRegex = new RegExp(`\\b${node.name}\\s*\\(`, 'g');
              if (callRegex.test(sourceCode)) {
                crossFileDependencies.push({
                  from: analysis.filename,
                  to: node.file,
                  type: 'function-call',
                  name: node.name,
                  targetType: node.type
                });
              }
            }
          });
        });
        
        analysisResult = {
          type: 'project-map',
          files: fileAnalyses,
          nodes: allNodes,
          dependencies: [...allDependencies, ...crossFileDependencies],
          stats: {
            totalFiles: files.length,
            totalFunctions: allNodes.filter(n => n.type === 'function').length,
            totalClasses: allNodes.filter(n => n.type === 'class').length,
            totalConstants: allNodes.filter(n => n.type === 'constant').length,
            totalDependencies: allDependencies.length + crossFileDependencies.length
          }
        };
        
      } else {
        // Single file analysis
        analysisResult = {
          type: 'single-file',
          ...analyzeCodeStructure(code, filename),
          stats: {
            lines: code.split('\n').length,
            characters: code.length
          }
        };
      }

      return res.status(200).json({
        ...analysisResult,
        timestamp: new Date().toISOString(),
        language
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