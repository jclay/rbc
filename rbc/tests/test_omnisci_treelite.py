import pytest
from rbc.tests import omnisci_fixture
from rbc.ctools import compile_ccode
from rbc.external import external


@pytest.fixture(scope='module')
def omnisci():
    for o in omnisci_fixture(globals(), minimal_version=(5, 5), debug=not True):
        yield o


def test_boston_house_prices(omnisci):
    device = 'cpu'
    import numpy as np
    import tempfile
    try:
        import treelite
    except ImportError as msg:
        pytest.skip(f'test requires treelite: {msg}')
    try:
        import xgboost
    except ImportError as msg:
        pytest.skip(f'test requires xgboost: {msg}')

    # Upload Boston house prices to server, notice that we collect all
    # row values expect the last one (MEDV) to a FLOAT array.
    import os
    csv_file = os.path.join(os.path.dirname(__file__), 'boston_house_prices.csv')
    data0 = []
    medv0 = []
    for i, line in enumerate(open(csv_file).readlines()):
        line = line.strip().replace(' ', '')
        if i == 0:
            header = line.split(',')
        else:
            row = list(map(float, line.split(',')))
            assert len(row) == len(header)
            data0.append(row[:-1])
            medv0.append(row[-1])
    table_name = f'{omnisci.table_name}bhp'
    omnisci.sql_execute(f'DROP TABLE IF EXISTS {table_name}')
    omnisci.sql_execute(f'CREATE TABLE IF NOT EXISTS {table_name} (data FLOAT[], medv FLOAT);')
    omnisci.load_table_columnar(table_name, data=data0, medv=medv0)
    # Get training data from server:
    descr, result = omnisci.sql_execute('SELECT rowid, data, medv FROM '
                                        f'{table_name} ORDER BY rowid LIMIT 50')
    result = list(result)
    medv = np.array([medv for _, data, medv in result])
    data = np.array([data for _, data, medv in result])
    assert len(medv) == 50

    # Train model using xgboost
    dtrain = xgboost.DMatrix(data, label=medv)
    params = {'max_depth': 3,
              'eta': 1,
              'objective': 'reg:squarederror',
              'eval_metric': 'rmse'}
    bst = xgboost.train(params, dtrain, len(medv), [(dtrain, 'train')])

    # Compile model to C
    working_dir = tempfile.mkdtemp()
    model = treelite.Model.from_xgboost(bst)
    model.compile(working_dir)
    model_c = open(os.path.join(working_dir, 'main.c')).read()
    # The C model implements
    #  float predict(union Entry* data, int pred_margin)
    # but we wrap it using
    model_c += '''
    float predict_float(float* data, int pred_margin) {
      return predict((union Entry*)data, pred_margin);
    }
    '''
    predict_float = external('float predict_float(float*, int32)')
    # to make UDF construction easier. Notice that predict_float can
    # be now called from a UDF.

    # Define predict function as UDF. Notice that the xgboost null
    # values are different from omniscidb null values, so we'll remap
    # before calling predict_float:
    null_value = np.int32(-1).view(np.float32)

    @omnisci('float(float[], int32)', devices=[device])
    def predict(data, pred_margin):
        for i in range(len(data)):
            if data.is_null(i):
                data[i] = null_value
        return predict_float(data.get_ptr(), pred_margin)

    # Compile C model to LLVM IR. In future, we might want this
    # compilation to happen in the server side as the client might not
    # have clang compiler installed.
    model_llvmir = compile_ccode(model_c, include_dirs=[working_dir])

    # RBC will link_in the LLVM IR module
    omnisci.user_defined_llvm_ir[device] = model_llvmir

    # Call predict on data in the server:
    descr, result = omnisci.sql_execute('SELECT rowid, predict(data, 2) FROM'
                                        f' {table_name} ORDER BY rowid')
    result = list(result)
    predict_medv = np.array([r[1] for r in result])

    # Clean up
    omnisci.sql_execute(f'DROP TABLE IF EXISTS {table_name}')

    # predict on the first 50 elements should be close to training labels
    error = abs(predict_medv[:len(medv)] - medv).max()/len(medv)
    assert error < 1e-4, error
