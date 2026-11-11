# **Untangling Co-Drift: Proactive Multi-Intent Failure Prediction and Root-Cause Disambiguation for Self-Driving Networks**

## Authors

Md. Kamrul Hossain (g202215400@kfupm.edu.sa) and Dr. Walid Aljoby (waleed.gobi@kfupm.edu.sa),

Information and Computer Science Department,
King Fahd University of Petroleum & Minerals (KFUPM),
Dhahran, Saudi Arabia.

## This is the official implementation for our paper submitted to IEEE Transactions on Network and Service Management (IEEE TNSM).

## Guide to Run

## For the Extended SDN testbed and O-RAN testbed implementations, please go to the respective folder for full guideline on how to run the codes.

## Below guideline is for Statistical Benchmark, Microservice Testbed and Simple SDN Edge-to-Cloud Testbed
1. Setup the dependencies
   
`python -m pip install --upgrade pip`

`pip install -r requirements.txt`

3. How to run: Find the "main_GitHub.ipynb" file in each experiment folder. Run it cell by cell to see the performance metrics of MILD and Baselines, as well as plots used in the main paper. Please update the dataset path for each dataset's experiment. Currently, data is kept separate in the Dataset folder.

